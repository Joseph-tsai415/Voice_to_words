"""Project persistence and every edit operation the UI can perform.

One project = one recording. It lives in data/projects/<id>/ as project.json
plus the normalised 16 kHz audio, so a project folder is self-contained and can
be copied elsewhere.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PROJECTS_DIR, SPEAKER_COLORS
from .textproc import DEFAULT_ZH_MODE

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def _path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------
def _read_json(path: Path, attempts: int = 5) -> dict[str, Any]:
    """Read a project file, retrying briefly around a concurrent save.

    save() swaps the file with os.replace(); on Windows a reader that opens it
    at that instant gets a PermissionError. Treating that as "this project does
    not exist" made projects flicker out of the sidebar while they were running.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.02 * (attempt + 1))
    raise last if last else OSError(f"無法讀取 {path}")


def load(project_id: str) -> dict[str, Any]:
    path = _path(project_id)
    if not path.exists():
        raise FileNotFoundError(f"找不到專案 {project_id}")
    return _read_json(path)


def save(project: dict[str, Any]) -> None:
    path = _path(project["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    project["updated_at"] = _now()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(project, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def list_projects() -> list[dict[str, Any]]:
    out = []
    if not PROJECTS_DIR.exists():
        return out
    for folder in PROJECTS_DIR.iterdir():
        meta = folder / "project.json"
        if not meta.exists():
            continue
        try:
            p = _read_json(meta)
        except (OSError, json.JSONDecodeError) as exc:
            # Only give up after the retries in _read_json; a project that
            # cannot be read is genuinely broken, so say so rather than
            # quietly dropping it from the list.
            out.append({
                "id": folder.name, "name": folder.name, "created_at": "",
                "updated_at": "", "duration": 0, "model": "",
                "status": "error", "error": f"專案檔無法讀取：{exc}",
                "num_segments": 0, "speakers": [],
            })
            continue
        out.append({
            "id": p["id"],
            "name": p.get("name", "未命名"),
            "created_at": p.get("created_at", ""),
            "updated_at": p.get("updated_at", ""),
            "duration": p.get("duration", 0),
            "model": p.get("model", ""),
            "status": p.get("status", "ready"),
            "error": p.get("error"),
            "num_segments": len(p.get("segments", [])),
            "speakers": [s["name"] for s in p.get("speakers", [])],
        })
    out.sort(key=lambda p: p.get("updated_at") or p.get("created_at") or "", reverse=True)
    return out


def delete(project_id: str) -> None:
    """Remove a project folder, and say so if it could not be removed.

    This used to be `rmtree(..., ignore_errors=True)`. On Windows the browser
    holds `audio.wav` open for playback, so the walk deleted `project.json`,
    hit the locked wav, swallowed the error and returned as though it had
    worked. The folder was then left with a 48 MB wav and no metadata - and
    `list_projects()` skips folders without a `project.json`, so the orphan
    was invisible in the UI and could never be cleared from it.

    The lock is transient (it goes once the audio element lets go), so retry
    briefly, then raise rather than lie about it.
    """
    folder = project_dir(project_id)
    last: OSError | None = None
    for attempt in range(4):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        try:
            shutil.rmtree(folder)      # again, this time telling us why
            return
        except OSError as exc:
            last = exc
            time.sleep(0.25 * (attempt + 1))
    if folder.exists():
        raise OSError(
            f"刪不掉 {folder.name}：{last}。"
            "檔案可能正被播放器或其他程式開著，關掉之後再試一次。"
        )


def find_orphans() -> list[dict[str, Any]]:
    """Project folders with no `project.json`.

    `list_projects()` skips these, so without this they are invisible: disk
    space that the UI cannot see and therefore cannot offer to reclaim. They
    come from a delete that only half-finished.
    """
    out: list[dict[str, Any]] = []
    if not PROJECTS_DIR.exists():
        return out
    for folder in PROJECTS_DIR.iterdir():
        if not folder.is_dir() or (folder / "project.json").exists():
            continue
        files = [f for f in folder.rglob("*") if f.is_file()]
        out.append({
            "id": folder.name,
            "files": [f.name for f in files],
            "bytes": sum(f.stat().st_size for f in files),
        })
    return out


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------
def create(name: str, duration: float, model: str, zh_mode: str = DEFAULT_ZH_MODE,
           options: dict | None = None) -> dict[str, Any]:
    project_id = _new_id("p")
    project = {
        "id": project_id,
        "name": name or "未命名會議",
        "created_at": _now(),
        "updated_at": _now(),
        "duration": round(float(duration), 2),
        "model": model,
        "zh_mode": zh_mode,
        "status": "queued",
        "audio": "audio.wav",
        # Kept so a failed run can be retried with the same settings.
        "options": options or {},
        "speakers": [],
        "segments": [],
    }
    project_dir(project_id).mkdir(parents=True, exist_ok=True)
    save(project)
    return project


def set_status(project_id: str, status: str, error: str | None = None) -> None:
    """Persist a lifecycle change. Never raises - it must not kill a worker."""
    try:
        with _lock_for(project_id):
            project = load(project_id)
            project["status"] = status
            if error:
                project["error"] = error
            elif status in ("processing", "queued", "ready"):
                project.pop("error", None)
            save(project)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass


def set_duration(project_id: str, seconds: float) -> None:
    try:
        with _lock_for(project_id):
            project = load(project_id)
            project["duration"] = round(float(seconds), 2)
            save(project)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass


def recover_interrupted() -> list[str]:
    """Mark jobs that died with the server as interrupted, not stuck-processing.

    Called at startup: without it a project killed mid-run shows a spinner
    forever, because the thread that owned it no longer exists.
    """
    touched = []
    for meta in list_projects():
        if meta["status"] in ("queued", "processing"):
            set_status(meta["id"], "interrupted",
                       "伺服器在辨識途中結束了，請按「重新辨識」。")
            touched.append(meta["id"])
    return touched


def clear_transcript(project_id: str, options: dict) -> dict[str, Any]:
    """Wipe the transcript so the project can be recognised again.

    Speakers go too: a different model or speaker count produces different
    clusters, and keeping the old names would attach them to nothing.
    """
    with _lock_for(project_id):
        project = load(project_id)
        project["segments"] = []
        project["speakers"] = []
        project["options"] = dict(options)
        project["model"] = options.get("model", project.get("model"))
        project["zh_mode"] = options.get("zh_mode", project.get("zh_mode"))
        project["status"] = "queued"
        project.pop("error", None)
        project["reruns"] = int(project.get("reruns", 0)) + 1
        save(project)
        return project


def replace_segment(project_id: str, segment_id: str, new_segments: list[dict],
                    extra_speakers: list[dict] | None = None) -> dict[str, Any]:
    """Swap one sentence for the sentences a re-run produced, in place."""
    with _lock_for(project_id):
        project = load(project_id)
        index, _ = _find_segment(project, segment_id)
        if extra_speakers is not None:
            project["speakers"] = extra_speakers
        project["segments"][index:index + 1] = new_segments
        project["segments"].sort(key=lambda s: s["start"])
        save(project)
        return project


def speaker_label(index: int) -> str:
    return f"講者 {index + 1}"


def _speaker_table(utterances: list[Any],
                   clusters: list[int] | None) -> tuple[list[dict], dict[int, str]]:
    """Build the speaker list, numbered in cluster order.

    `clusters` is the *complete* set diarization found. Pass it whenever the
    utterance list might still grow: deriving the table from a partial list
    renumbers everyone the moment a batch introduces a lower cluster id, so
    the names the user is watching would shuffle under them mid-run.
    """
    cluster_ids = sorted(clusters) if clusters else sorted({u.speaker for u in utterances})
    speakers: list[dict] = []
    index_of: dict[int, str] = {}
    for i, cluster in enumerate(cluster_ids):
        sid = f"S{i + 1}"
        index_of[cluster] = sid
        speakers.append({
            "id": sid,
            "name": speaker_label(i),
            "color": SPEAKER_COLORS[i % len(SPEAKER_COLORS)],
            "cluster": cluster,
        })
    return speakers, index_of


def _segments_from(utterances: list[Any], speakers: list[dict],
                   index_of: dict[int, str]) -> list[dict]:
    fallback = speakers[0]["id"] if speakers else "S1"
    return [
        {
            "id": f"seg-{i:04d}",
            "start": u.start,
            "end": u.end,
            "speaker": index_of.get(u.speaker, fallback),
            "text": u.text,
            "original_text": u.text,
            "original_speaker": index_of.get(u.speaker, ""),
            "edited": False,
            "speaker_edited": False,
        }
        for i, u in enumerate(utterances)
    ]


def populate(project_id: str, utterances: list[Any],
             clusters: list[int] | None = None) -> dict[str, Any]:
    """Fill a project with finished pipeline output and mark it ready."""
    with _lock_for(project_id):
        project = load(project_id)
        speakers, index_of = _speaker_table(utterances, clusters)
        project["speakers"] = speakers
        project["segments"] = _segments_from(utterances, speakers, index_of)
        project["status"] = "ready"
        project.pop("error", None)
        save(project)
        return project


def populate_progress(project_id: str, utterances: list[Any],
                      clusters: list[int]) -> dict[str, Any]:
    """Write what has been decoded so far, leaving the job running.

    Deliberately does **not** touch `status`: the project stays `processing`
    so the queue, the stall detector and the frontend all keep treating it as
    in-flight. This only exists so the user can read the first half of a long
    meeting instead of watching a placeholder for several minutes.
    """
    with _lock_for(project_id):
        project = load(project_id)
        if project.get("status") not in ("processing", "queued"):
            # Cancelled or already finished while this batch was decoding -
            # writing now would resurrect a transcript the user discarded.
            return project
        speakers, index_of = _speaker_table(utterances, clusters)
        project["speakers"] = speakers
        project["segments"] = _segments_from(utterances, speakers, index_of)
        project["partial"] = True
        save(project)
        return project


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------
def _find_segment(project: dict, segment_id: str) -> tuple[int, dict]:
    for i, seg in enumerate(project["segments"]):
        if seg["id"] == segment_id:
            return i, seg
    raise KeyError(f"找不到句子 {segment_id}")


def rename_project(project_id: str, name: str) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        project["name"] = name.strip() or project["name"]
        save(project)
        return project


def rename_speaker(project_id: str, speaker_id: str, name: str) -> dict:
    """Rename one speaker everywhere - this is the 'Speaker 1 是 Joseph' step."""
    with _lock_for(project_id):
        project = load(project_id)
        for spk in project["speakers"]:
            if spk["id"] == speaker_id:
                spk["name"] = name.strip() or spk["name"]
                break
        else:
            raise KeyError(f"找不到講者 {speaker_id}")
        save(project)
        return project


def set_speaker_color(project_id: str, speaker_id: str, color: str) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        for spk in project["speakers"]:
            if spk["id"] == speaker_id:
                spk["color"] = color
                break
        save(project)
        return project


def add_speaker(project_id: str, name: str = "") -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        used = {s["id"] for s in project["speakers"]}
        n = 1
        while f"S{n}" in used:
            n += 1
        idx = len(project["speakers"])
        project["speakers"].append({
            "id": f"S{n}",
            "name": name.strip() or speaker_label(idx),
            "color": SPEAKER_COLORS[idx % len(SPEAKER_COLORS)],
            "cluster": -1,
        })
        save(project)
        return project


def delete_speaker(project_id: str, speaker_id: str, reassign_to: str | None = None) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        if len(project["speakers"]) <= 1:
            raise ValueError("至少要保留一位講者。")
        remaining = [s for s in project["speakers"] if s["id"] != speaker_id]
        target = reassign_to or remaining[0]["id"]
        for seg in project["segments"]:
            if seg["speaker"] == speaker_id:
                seg["speaker"] = target
                seg["speaker_edited"] = True
        project["speakers"] = remaining
        save(project)
        return project


def set_segment_speaker(project_id: str, segment_id: str, speaker_id: str) -> dict:
    """Re-attribute a single sentence - the core correction the tool exists for."""
    with _lock_for(project_id):
        project = load(project_id)
        if speaker_id not in {s["id"] for s in project["speakers"]}:
            raise KeyError(f"找不到講者 {speaker_id}")
        _, seg = _find_segment(project, segment_id)
        seg["speaker"] = speaker_id
        seg["speaker_edited"] = speaker_id != seg.get("original_speaker")
        save(project)
        return project


def set_segment_text(project_id: str, segment_id: str, text: str) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        _, seg = _find_segment(project, segment_id)
        seg["text"] = text
        seg["edited"] = text.strip() != (seg.get("original_text") or "").strip()
        save(project)
        return project


def bulk_set_speaker(project_id: str, segment_ids: list[str], speaker_id: str) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        if speaker_id not in {s["id"] for s in project["speakers"]}:
            raise KeyError(f"找不到講者 {speaker_id}")
        wanted = set(segment_ids)
        for seg in project["segments"]:
            if seg["id"] in wanted:
                seg["speaker"] = speaker_id
                seg["speaker_edited"] = speaker_id != seg.get("original_speaker")
        save(project)
        return project


def swap_speakers(project_id: str, a: str, b: str) -> dict:
    """Whole-transcript fix for when diarization mirrored two people."""
    with _lock_for(project_id):
        project = load(project_id)
        ids = {s["id"] for s in project["speakers"]}
        if a not in ids or b not in ids:
            raise KeyError("講者不存在。")
        for seg in project["segments"]:
            if seg["speaker"] == a:
                seg["speaker"] = b
                seg["speaker_edited"] = True
            elif seg["speaker"] == b:
                seg["speaker"] = a
                seg["speaker_edited"] = True
        save(project)
        return project


def merge_with_next(project_id: str, segment_id: str) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        i, seg = _find_segment(project, segment_id)
        if i + 1 >= len(project["segments"]):
            raise ValueError("這已經是最後一句，無法合併。")
        nxt = project["segments"][i + 1]
        joiner = "" if re.search(r"[一-鿿]$", seg["text"]) else " "
        seg["text"] = (seg["text"].rstrip() + joiner + nxt["text"].lstrip()).strip()
        seg["end"] = nxt["end"]
        seg["edited"] = True
        del project["segments"][i + 1]
        save(project)
        return project


def split_segment(project_id: str, segment_id: str, char_index: int) -> dict:
    """Split one sentence in two, apportioning the time range by character count."""
    with _lock_for(project_id):
        project = load(project_id)
        i, seg = _find_segment(project, segment_id)
        text = seg["text"]
        char_index = max(1, min(int(char_index), len(text) - 1))
        head, tail = text[:char_index].strip(), text[char_index:].strip()
        if not head or not tail:
            raise ValueError("切分點必須在句子中間。")

        ratio = char_index / len(text)
        boundary = round(seg["start"] + (seg["end"] - seg["start"]) * ratio, 3)
        new_seg = {
            "id": _new_id("seg"),
            "start": boundary,
            "end": seg["end"],
            "speaker": seg["speaker"],
            "text": tail,
            "original_text": tail,
            "original_speaker": seg.get("original_speaker", ""),
            "edited": True,
            "speaker_edited": seg.get("speaker_edited", False),
        }
        seg["text"] = head
        seg["end"] = boundary
        seg["edited"] = True
        project["segments"].insert(i + 1, new_seg)
        save(project)
        return project


def delete_segment(project_id: str, segment_id: str) -> dict:
    with _lock_for(project_id):
        project = load(project_id)
        i, _ = _find_segment(project, segment_id)
        del project["segments"][i]
        save(project)
        return project


def set_zh_mode(project_id: str, zh_mode: str) -> dict:
    """Re-run Chinese conversion over text that has not been hand-edited."""
    from .textproc import convert_zh

    with _lock_for(project_id):
        project = load(project_id)
        project["zh_mode"] = zh_mode
        for seg in project["segments"]:
            if not seg.get("edited"):
                seg["text"] = convert_zh(seg.get("original_text", seg["text"]), zh_mode)
        save(project)
        return project


# --------------------------------------------------------------------------
# Derived views
# --------------------------------------------------------------------------
def bubbles(project: dict, gap: float) -> list[dict]:
    """Group consecutive same-speaker segments into chat bubbles for the UI."""
    groups: list[dict] = []
    for seg in project["segments"]:
        if groups and groups[-1]["speaker"] == seg["speaker"] \
                and seg["start"] - groups[-1]["end"] <= gap:
            groups[-1]["segments"].append(seg)
            groups[-1]["end"] = seg["end"]
        else:
            groups.append({
                "speaker": seg["speaker"],
                "start": seg["start"],
                "end": seg["end"],
                "segments": [seg],
            })
    return groups


def speaker_map(project: dict) -> dict[str, dict]:
    return {s["id"]: s for s in project["speakers"]}
