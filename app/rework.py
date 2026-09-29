"""Re-running recognition: a whole project, or one sentence on its own.

Two situations this covers:

* The model or the speaker count was wrong for the whole meeting. Re-run it with
  different settings; the transcript is replaced.
* One sentence actually contains several people. Re-run just that time range with
  the real speaker count and replace it with the sentences that come out. New
  pieces are matched back to the project's existing speakers by voiceprint, so
  they land under the right names rather than as strangers.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from . import asr, project as store, voiceprint
from .audio import load_16k_mono
from .config import (ASR_MODELS, DEFAULT_MODEL, DEFAULT_THREADS, SAMPLE_RATE,
                     SPEAKER_COLORS, resolve_language)
from .textproc import DEFAULT_ZH_MODE

log = logging.getLogger("scribe.rework")

# Below this the slice has no room for two speakers to alternate meaningfully.
MIN_SLICE_SEC = 0.8


def audio_path(project_id: str):
    return store.project_dir(project_id) / "audio.wav"


def options_from(project: dict, overrides: dict | None = None) -> dict[str, Any]:
    """Merge a project's stored settings with whatever the user just chose."""
    base = dict(project.get("options") or {})
    base.setdefault("model", project.get("model", DEFAULT_MODEL))
    base.setdefault("zh_mode", project.get("zh_mode", DEFAULT_ZH_MODE))
    base.setdefault("num_speakers", -1)
    base.setdefault("threads", DEFAULT_THREADS)
    base.setdefault("language", "")

    for key, value in (overrides or {}).items():
        if value is None or value == "":
            continue
        base[key] = value

    # Unknown keys, and add-ons like the punctuation model that sit in the
    # catalogue but cannot decode, both fall back to the default recogniser.
    if (base["model"] not in ASR_MODELS
            or ASR_MODELS[base["model"]].get("role", "asr") != "asr"):
        base["model"] = DEFAULT_MODEL
    base["language"] = resolve_language(base["model"], base.get("language", ""))
    try:
        base["num_speakers"] = int(base["num_speakers"])
    except (TypeError, ValueError):
        base["num_speakers"] = -1
    try:
        base["threads"] = max(1, int(base["threads"]))
    except (TypeError, ValueError):
        base["threads"] = DEFAULT_THREADS
    return base


# ---------------------------------------------------------------------------
# Whole project
# ---------------------------------------------------------------------------
def prepare_full_rerun(project_id: str, overrides: dict | None = None) -> dict[str, Any]:
    """Clear the transcript and stage the project for another pass.

    The normalised audio.wav is the input, so this still works after the
    original upload has been cleaned up.
    """
    project = store.load(project_id)
    wav = audio_path(project_id)
    if not wav.exists():
        raise FileNotFoundError("找不到這個專案的音檔，無法重新辨識。")

    options = options_from(project, overrides)
    store.clear_transcript(project_id, options)
    log.info("專案 %s 重新辨識：%s", project_id,
             ", ".join(f"{k}={v}" for k, v in options.items()))
    return options


# ---------------------------------------------------------------------------
# One sentence
# ---------------------------------------------------------------------------
def rerun_segment(project_id: str, segment_id: str, num_speakers: int = -1,
                  overrides: dict | None = None) -> dict[str, Any]:
    """Re-recognise a single sentence and replace it with what comes out.

    Returns the updated project plus a short summary of what happened.
    """
    project = store.load(project_id)
    index, segment = store._find_segment(project, segment_id)   # noqa: SLF001

    wav = audio_path(project_id)
    if not wav.exists():
        raise FileNotFoundError("找不到這個專案的音檔，無法重新辨識。")

    start, end = float(segment["start"]), float(segment["end"])
    if end - start < MIN_SLICE_SEC:
        raise ValueError(f"這一句只有 {end - start:.1f} 秒，太短了，無法再拆分。")

    options = options_from(project, overrides)
    samples = load_16k_mono(wav)
    clip = samples[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
    if clip.size < int(MIN_SLICE_SEC * SAMPLE_RATE):
        raise ValueError("音檔裡找不到這一句對應的聲音。")

    wanted = int(num_speakers) if num_speakers and num_speakers > 0 else -1
    log.info("重新辨識 %s 的 %s（%.2f–%.2f 秒，指定 %s 位講者）",
             project_id, segment_id, start, end,
             wanted if wanted > 0 else "自動")

    # Diarize just this slice in a child process - process() holds the GIL and
    # would freeze the web server for the duration (see diarize_worker.py).
    from .diarize_worker import DiarizationFailed, diarize_in_process
    try:
        turns = diarize_in_process(wav, num_speakers=wanted,
                                   num_threads=options["threads"],
                                   start=start, end=end)
    except DiarizationFailed as exc:
        log.warning("子行程講者分離失敗（%s），改用同行程執行", exc)
        turns = asr.diarize(clip, num_speakers=wanted, num_threads=options["threads"])
    if turns:
        units = asr.split_by_speaker(asr.vad_chunks(clip), turns)
    else:
        units = []
    if not units:
        # VAD found nothing to keep; fall back to the whole slice as one piece.
        units = [(0.0, len(clip) / SAMPLE_RATE, 0)]

    recognizer = asr.build_recognizer(options["model"],
                                      num_threads=options["threads"],
                                      language=options["language"])
    from .textproc import clean

    pieces: list[tuple[float, float, int]] = []
    texts: list[str] = []
    for piece_start, piece_end, cluster in units:
        audio = clip[int(piece_start * SAMPLE_RATE):int(piece_end * SAMPLE_RATE)]
        if audio.size < int(0.1 * SAMPLE_RATE):
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        recognizer.decode_stream(stream)
        text = clean(stream.result.text or "", options["zh_mode"])
        if not text:
            continue
        pieces.append((piece_start, piece_end, cluster))
        texts.append(text)

    if not pieces:
        raise ValueError("重新辨識後沒有得到任何文字，原本的句子保持不變。")

    # Put the new pieces under the speakers this project already knows.
    profiles = voiceprint.build_profiles(project, samples, exclude_segment=segment_id)
    matches = voiceprint.assign(pieces, clip, profiles)

    speakers = {s["id"]: s for s in project["speakers"]}
    used_ids = set(speakers)
    cluster_to_speaker: dict[int, str] = {}
    created: list[str] = []

    # Whoever the sentence was attributed to keeps the largest piece when the
    # voiceprint cannot decide. Turning every piece into a new stranger is the
    # wrong default: the original speaker almost certainly said one of them.
    duration_by_cluster: dict[int, float] = {}
    for piece_start, piece_end, cluster in pieces:
        duration_by_cluster[cluster] = (
            duration_by_cluster.get(cluster, 0.0) + (piece_end - piece_start))
    biggest = max(duration_by_cluster, key=duration_by_cluster.get)

    def add_speaker() -> str:
        number = 1
        while f"S{number}" in used_ids:
            number += 1
        new_id = f"S{number}"
        used_ids.add(new_id)
        project["speakers"].append({
            "id": new_id,
            "name": store.speaker_label(len(project["speakers"])),
            "color": SPEAKER_COLORS[len(project["speakers"]) % len(SPEAKER_COLORS)],
            "cluster": -1,
        })
        speakers[new_id] = project["speakers"][-1]
        created.append(new_id)
        return new_id

    matched_speakers = {m.get("speaker") for m in matches if m.get("speaker")}
    for (_, _, cluster), match in zip(pieces, matches):
        if cluster in cluster_to_speaker:
            continue
        speaker = match.get("speaker")
        if speaker and speaker in speakers:
            cluster_to_speaker[cluster] = speaker
        elif cluster == biggest and segment["speaker"] not in matched_speakers \
                and segment["speaker"] in speakers:
            cluster_to_speaker[cluster] = segment["speaker"]
        else:
            cluster_to_speaker[cluster] = add_speaker()

    new_segments = []
    for i, ((piece_start, piece_end, cluster), text, match) in enumerate(
            zip(pieces, texts, matches)):
        new_segments.append({
            "id": f"{segment_id}-r{i}" if i else segment_id,
            "start": round(start + piece_start, 3),
            "end": round(start + piece_end, 3),
            "speaker": cluster_to_speaker[cluster],
            "text": text,
            "original_text": text,
            "original_speaker": cluster_to_speaker[cluster],
            "edited": False,
            "speaker_edited": False,
            "match_score": match.get("score"),
        })

    updated = store.replace_segment(project_id, segment_id, new_segments,
                                    extra_speakers=project["speakers"])
    log.info("  %s 拆成 %d 句，對應到 %d 位講者%s", segment_id, len(new_segments),
             len(set(cluster_to_speaker.values())),
             f"，新增 {len(created)} 位" if created else "")

    return {
        "project": updated,
        "summary": {
            "pieces": len(new_segments),
            "speakers": sorted({s["speaker"] for s in new_segments}),
            "new_speakers": created,
            "scores": [s.get("match_score") for s in new_segments],
        },
    }
