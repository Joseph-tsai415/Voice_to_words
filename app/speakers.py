"""A voiceprint library, so S1 is Jimmy again next time.

Speaker ids are per-project and meaningless across runs: diarization hands
out cluster numbers in whatever order it happens to find them, so re-running
a meeting throws away every name that was typed. The embeddings that would
fix this already existed - [voiceprint.py](voiceprint.py) builds them to put
re-run sentences back under the right speaker - they were simply never kept.

This keeps one named unit vector per person in `data/speakers.json`. Naming a
speaker remembers them; a later meeting matches each cluster against the
library and fills the name in.

On the representation: a 512-float unit vector is 5.3 KB and comparison is a
single dot product, so there is nothing to gain by compressing it and
accuracy to lose. What actually decides whether a match is right is how much
and which audio went into it - `build_profiles()` takes the longest segments
first and averages several - not the shape of the vector.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .config import DATA_DIR
from .voiceprint import MATCH_THRESHOLD, best_match

log = logging.getLogger("scribe.speakers")

LIBRARY_PATH = DATA_DIR / "speakers.json"

_guard = threading.Lock()
_cache: dict[str, dict[str, Any]] | None = None

# "講者 3", "Speaker 2" and friends are what the pipeline calls someone it
# does not know. Storing a voiceprint under one of those would be storing it
# under a name that means a different person in every other project.
_PLACEHOLDER = re.compile(r"^\s*(講者|说话人|說話人|speaker|spk)\s*\d*\s*$", re.I)


def is_placeholder(name: str) -> bool:
    """True when this is a stand-in label rather than someone's name."""
    return not (name or "").strip() or bool(_PLACEHOLDER.match(name))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        people = raw.get("people") or {}
        _cache = {k: v for k, v in people.items() if v.get("vector")}
    except (OSError, json.JSONDecodeError, AttributeError):
        # A missing or unreadable library is not an error; it just means
        # nobody has been remembered yet.
        _cache = {}
    return _cache


def _write() -> None:
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"people": _cache or {}}, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(LIBRARY_PATH)          # same atomic swap the project store uses


def reload() -> None:
    """Drop the in-memory copy; the next read goes back to disk."""
    global _cache
    with _guard:
        _cache = None


def load_library() -> dict[str, np.ndarray]:
    """Name -> unit vector, ready for `best_match`."""
    with _guard:
        return {name: np.asarray(rec["vector"], dtype=np.float32)
                for name, rec in _read().items()}


def sample_count(name: str) -> int:
    with _guard:
        return int((_read().get(name) or {}).get("samples", 0))


def remember(name: str, vector: np.ndarray | None) -> bool:
    """Add or refine this person's voiceprint. False if not worth storing.

    Re-remembering the same name averages the new vector into the old one
    rather than replacing it, so a person recorded across several meetings
    ends up with a steadier print than any single clip would give.
    """
    if vector is None or is_placeholder(name):
        return False
    name = name.strip()
    v = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    if not np.isfinite(norm) or norm <= 0:
        return False
    v = v / norm

    with _guard:
        people = _read()
        old = people.get(name)
        if old:
            n = max(1, int(old.get("samples", 1)))
            prev = np.asarray(old["vector"], dtype=np.float32)
            merged = (prev * n + v) / (n + 1)
            m = float(np.linalg.norm(merged))
            v = merged / m if m > 0 else v
            samples = n + 1
        else:
            samples = 1
        people[name] = {"vector": [round(float(x), 6) for x in v],
                        "samples": samples, "updated_at": _now()}
        _write()
    log.info("記住講者聲紋：%s（第 %d 次）", name, samples)
    return True


def profile_for(wav_path: Any, segments: list[dict], speaker_id: str,
                max_sec: float = 30.0) -> np.ndarray | None:
    """Voiceprint for one speaker, reading only that speaker's audio.

    `voiceprint.build_profiles()` wants the whole decoded recording, which is
    ~130 MB for an hour - far too much to load because somebody typed a name.
    This seeks to the clips it needs instead. Longest segments first, for the
    same reason build_profiles does it: they are the clearest examples of a
    voice, and a short backchannel ("嗯") carries almost no speaker identity.
    """
    import soundfile as sf

    from .config import SAMPLE_RATE
    from .voiceprint import embed

    mine = sorted((s for s in segments if s.get("speaker") == speaker_id),
                  key=lambda s: s["end"] - s["start"], reverse=True)
    if not mine:
        return None

    vectors: list[np.ndarray] = []
    used = 0.0
    try:
        info = sf.info(str(wav_path))
    except Exception as exc:
        log.warning("讀不到音檔，無法建立聲紋：%s", exc)
        return None

    for seg in mine:
        if used >= max_sec:
            break
        start = int(seg["start"] * info.samplerate)
        frames = int((seg["end"] - seg["start"]) * info.samplerate)
        if frames <= 0:
            continue
        try:
            clip, _ = sf.read(str(wav_path), start=start, frames=frames,
                              dtype="float32", always_2d=False)
        except Exception:
            continue
        if info.samplerate != SAMPLE_RATE or clip.size == 0:
            continue
        vector = embed(np.asarray(clip, dtype=np.float32))
        if vector is None:
            continue
        vectors.append(vector)
        used += seg["end"] - seg["start"]

    if not vectors:
        return None
    mean = np.mean(vectors, axis=0)
    norm = float(np.linalg.norm(mean))
    return (mean / norm) if norm > 0 else None


def identify(vector: np.ndarray | None) -> tuple[str | None, float]:
    """Closest remembered person, or (None, score) if nobody is close enough.

    Threshold and the measurement behind it live in voiceprint.py.
    """
    return best_match(vector, load_library())


def forget(name: str) -> bool:
    with _guard:
        people = _read()
        if name not in people:
            return False
        del people[name]
        _write()
    log.info("已忘記講者聲紋：%s", name)
    return True


def describe() -> list[dict[str, Any]]:
    """The library as the UI shows it - no vectors."""
    with _guard:
        return sorted(
            ({"name": n, "samples": int(r.get("samples", 1)),
              "updated_at": r.get("updated_at", "")}
             for n, r in _read().items()),
            key=lambda r: r["name"])


__all__ = ["LIBRARY_PATH", "MATCH_THRESHOLD", "describe", "forget", "identify",
           "is_placeholder", "load_library", "reload", "remember",
           "sample_count"]
