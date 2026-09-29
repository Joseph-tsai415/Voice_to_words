"""Speech pipeline: who spoke when (diarization) x what was said (ASR).

The two are computed independently and then intersected, so a single VAD chunk
that spans a speaker change gets split at the boundary instead of being credited
to whoever happened to talk longest.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable

import numpy as np
import sherpa_onnx

from . import config as C
from .config import ASR_MODELS, SAMPLE_RATE, model_paths, resolve_language
from .gpu import resolve as resolve_provider
from .textproc import (DEFAULT_ZH_MODE, clean, is_backchannel,
                       is_meaningful, looks_unfinished)

log = logging.getLogger("scribe.asr")

ProgressFn = Callable[[str, float, str], None]   # (stage, fraction 0-1, detail)


@dataclass
class Utterance:
    start: float
    end: float
    speaker: int          # cluster index from diarization
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Model construction (cached - loading a 1 GB ONNX graph takes seconds)
# --------------------------------------------------------------------------
# (key, language) -> (recognizer, threads_it_was_built_with)
_recognizer_cache: dict[tuple, tuple[sherpa_onnx.OfflineRecognizer, int]] = {}
_cache_lock = threading.Lock()
# One build at a time per model: three jobs starting together would otherwise
# each allocate a full ~1 GB copy before any of them reaches the cache.
_build_locks: dict[tuple, threading.Lock] = {}

# Headroom left on the GPU for activations and the diarization models.
VRAM_RESERVE_MB = 2048

# Loaded recognisers are kept. On the CPU, eviction happens only if
# SCRIBE_MODEL_RAM_MB asks for it; on the GPU, VRAM is a hard ceiling.
_last_used: dict[tuple, float] = {}
_in_use: dict[tuple, int] = {}


def model_bytes(key: str) -> int:
    """On-disk size, a good enough proxy for how much RAM it will occupy.

    Counts every required file, not just the ones passed to the factory: Cohere
    Transcribe's weights live in a 2.7 GB external-data blob listed under
    extra_files, so measuring only the role paths reported 156 MB for a model
    that actually needs 2.9 GB - and eviction sized itself on that.
    """
    from .config import model_dir, required_files

    base = model_dir(key)
    total = 0
    try:
        for rel in required_files(key):
            path = base / rel
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        return total
    return total


def loaded_models() -> list[dict]:
    with _cache_lock:
        return [{"key": k[0], "language": k[1], "mb": round(model_bytes(k[0]) / 1e6),
                 "in_use": _in_use.get(k, 0)} for k in _recognizer_cache]


def loaded_bytes() -> int:
    with _cache_lock:
        return sum(model_bytes(k[0]) for k in _recognizer_cache)


def _evict_locked(need_bytes: int) -> None:
    """Drop idle recognisers only when an explicit cap has been configured.

    By default nothing is evicted. The operating system already handles this:
    an idle model's pages are written to the page file and never read back,
    which costs page-file space, not speed. Measured on a 32 GB machine with a
    49 GB SSD page file while two big jobs ran: 16 pages/sec, 3 MB/s of disk -
    CPU-bound throughout.

    SCRIBE_MODEL_RAM_MB caps it anyway, for a machine with no page file.
    """
    from .memory import configured_model_cap

    budget = configured_model_cap()

    # Video memory has no page file. System RAM can be overcommitted and the OS
    # simply pages; VRAM cannot, and loading a second big model on top of a
    # full GPU fails hard with "CUDA failure 2: out of memory". So when the
    # models are on the GPU, its capacity is a real ceiling regardless of
    # whether the user configured one.
    provider, _ = resolve_provider(C.PROVIDER)
    if provider == "cuda":
        from .gpu import vram_mb

        vram = vram_mb()
        if vram:
            total_mb = vram[0]
            usable = int((total_mb - VRAM_RESERVE_MB) * 1024 ** 2)
            budget = usable if budget is None else min(budget, usable)

    if budget is None:
        return

    current = sum(model_bytes(k[0]) for k in _recognizer_cache)
    if current + need_bytes <= budget:
        return

    idle = sorted((k for k in _recognizer_cache if not _in_use.get(k)),
                  key=lambda k: _last_used.get(k, 0.0))
    for cache_key in idle:
        if current + need_bytes <= budget:
            break
        freed = model_bytes(cache_key[0])
        _recognizer_cache.pop(cache_key, None)
        _last_used.pop(cache_key, None)
        current -= freed
        log.info("釋放模型 %s（%.0f MB）以騰出空間，上限 %.1f GB",
                 cache_key[0], freed / 1e6, budget / 1e9)
    import gc
    gc.collect()


class use_recognizer:
    """Borrow a recogniser and stop it being evicted while it is decoding."""

    def __init__(self, key: str, num_threads: int = 4, language: str = "") -> None:
        self.key = key
        self.num_threads = num_threads
        self.language = language
        self.cache_key: tuple | None = None
        self.recognizer: sherpa_onnx.OfflineRecognizer | None = None

    def __enter__(self) -> sherpa_onnx.OfflineRecognizer:
        self.recognizer = build_recognizer(self.key, self.num_threads, self.language)
        self.cache_key = (self.key, resolve_language(self.key, self.language))
        with _cache_lock:
            _in_use[self.cache_key] = _in_use.get(self.cache_key, 0) + 1
            _last_used[self.cache_key] = time.time()
        return self.recognizer

    def __exit__(self, *exc) -> bool:
        with _cache_lock:
            if self.cache_key is not None:
                remaining = _in_use.get(self.cache_key, 1) - 1
                if remaining > 0:
                    _in_use[self.cache_key] = remaining
                else:
                    _in_use.pop(self.cache_key, None)
                _last_used[self.cache_key] = time.time()
        return False


def build_recognizer(key: str, num_threads: int = 4, language: str = "") -> sherpa_onnx.OfflineRecognizer:
    spec = ASR_MODELS.get(key)
    if spec is None:
        raise KeyError("未知的模型：" + str(key))
    missing = [str(p) for p in model_paths(key).values() if not p.exists()]
    if missing:
        raise FileNotFoundError(spec["label"] + " 尚未下載，缺少：" + missing[0])

    # Never hand a required-language model an empty string: it decodes every
    # stream to nothing and reports success.
    language = resolve_language(key, language)

    # Deliberately NOT keyed on num_threads. A recognizer is ~1 GB resident and
    # is safe to share across concurrent jobs (each gets its own stream), so
    # keying on the thread count would load a second full copy just because one
    # project asked for 4 threads and another for 6 - three concurrent jobs
    # would then need 3 GB and thrash the machine.
    cache_key = (key, language)
    with _cache_lock:
        cached = _recognizer_cache.get(cache_key)
        if cached is not None:
            if cached[1] != num_threads:
                log.debug("模型 %s 已以 %d 執行緒載入，沿用同一份（省記憶體）",
                          key, cached[1])
            _last_used[cache_key] = time.time()
            return cached[0]

    with _cache_lock:
        build_lock = _build_locks.setdefault(cache_key, threading.Lock())

    with build_lock:
        # Someone may have built it while we waited for the lock.
        with _cache_lock:
            cached = _recognizer_cache.get(cache_key)
            if cached is not None:
                _last_used[cache_key] = time.time()
                return cached[0]
            _evict_locked(model_bytes(key))
        return _build(key, spec, cache_key, num_threads, language)


def _build(key: str, spec: dict, cache_key: tuple, num_threads: int,
           language: str) -> sherpa_onnx.OfflineRecognizer:
    total_mb = sum(p.stat().st_size for p in model_paths(key).values()
                   if p.is_file()) / 1e6
    log.info("載入模型 %s（%.0f MB，%d 執行緒）…", key, total_mb, num_threads)

    provider, why = resolve_provider(C.PROVIDER)
    log.info("  執行裝置：%s（%s）", provider, why)

    p = {role: str(path) for role, path in model_paths(key).items()}
    kind = spec["kind"]

    if kind == "sense_voice":
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=p["model"], tokens=p["tokens"], num_threads=num_threads,
            language=language, use_itn=True, provider=provider,
        )
    elif kind == "transducer":
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=p["encoder"], decoder=p["decoder"], joiner=p["joiner"],
            tokens=p["tokens"], num_threads=num_threads, provider=provider,
        )
    elif kind == "paraformer":
        rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=p["model"], tokens=p["tokens"], num_threads=num_threads,
            provider=provider,
        )
    elif kind == "fire_red_asr_ctc":
        rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr_ctc(
            model=p["model"], tokens=p["tokens"], num_threads=num_threads,
            provider=provider,
        )
    elif kind == "fire_red_asr":
        rec = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
            encoder=p["encoder"], decoder=p["decoder"], tokens=p["tokens"],
            num_threads=num_threads, provider=provider,
        )
    elif kind == "qwen3":
        rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=p["conv_frontend"], encoder=p["encoder"],
            decoder=p["decoder"], tokenizer=p["tokenizer"], num_threads=num_threads,
            provider=provider,
        )
    elif kind == "cohere":
        rec = sherpa_onnx.OfflineRecognizer.from_cohere_transcribe(
            encoder=p["encoder"], decoder=p["decoder"], tokens=p["tokens"],
            num_threads=num_threads, language=language, use_punct=True,
            use_itn=True, provider=provider,
        )
    else:
        raise ValueError("不支援的模型類型：" + str(kind))

    with _cache_lock:
        _recognizer_cache[cache_key] = (rec, num_threads)
        _last_used[cache_key] = time.time()
    return rec


def unload_recognizers() -> None:
    with _cache_lock:
        _recognizer_cache.clear()
        _last_used.clear()
        _in_use.clear()
    import gc
    gc.collect()


# --------------------------------------------------------------------------
# Stage 1 - who spoke when
# --------------------------------------------------------------------------
def diarize(samples: np.ndarray, num_speakers: int = -1, threshold: float = 0.5,
            num_threads: int = 2, progress: ProgressFn | None = None,
            cancelled: Callable[[], bool] | None = None) -> list[tuple[float, float, int]]:
    """Return (start, end, speaker_index) turns, sorted by time.

    num_speakers <= 0 lets the clusterer decide using threshold; a positive
    value pins the count, which is far more reliable when you know the room.

    This is the long pole on a long recording, so it reports chunk-level
    progress - without it the UI sits on one label for minutes and looks hung.
    """
    # See config.DIARIZE_PROVIDER: the GPU measured slower for this stage.
    provider, _ = resolve_provider(C.DIARIZE_PROVIDER)
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(C.SEGMENTATION_MODEL)
            ),
            num_threads=num_threads, provider=provider,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(C.SPEAKER_EMBEDDING_MODEL), num_threads=num_threads,
            provider=provider,
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=int(num_speakers) if num_speakers and num_speakers > 0 else -1,
            threshold=float(threshold),
        ),
        min_duration_on=0.25,
        min_duration_off=0.4,
    )
    if not cfg.validate():
        raise RuntimeError("講者分離設定無效，請確認 models/ 內的模型檔。")

    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def on_chunk(done: int, total: int) -> int:
        if cancelled and cancelled():
            return 1                      # non-zero aborts the C++ side
        if progress and total:
            progress("diarize", done / total,
                     f"分析音訊 {done} / {total} 個區段")
        return 0

    result = sd.process(samples, callback=on_chunk).sort_by_start_time()
    turns = [(float(s.start), float(s.end), int(s.speaker)) for s in result]

    # Threshold clustering over-splits on real recordings: a 25-minute meeting
    # with three people came back as 102 speakers. Compare whole clusters by
    # voiceprint and join the ones that are plainly the same person. Skipped
    # when the speaker count was pinned - diarization already honoured it.
    if num_speakers <= 0 and turns:
        from . import voiceprint
        if progress:
            progress("diarize", 0.98, "比對聲紋、合併重複的講者…")
        try:
            turns, merges = voiceprint.merge_similar_clusters(turns, samples)
        except Exception as exc:            # never lose a transcript over this
            log.warning("聲紋合併失敗，沿用原始分群：%s", exc)
    return turns


def _diarize_maybe_out_of_process(samples: np.ndarray, wav_path: str | None, *,
                                  num_speakers: int, threshold: float,
                                  num_threads: int,
                                  progress: ProgressFn | None,
                                  cancelled: Callable[[], bool] | None
                                  ) -> list[tuple[float, float, int]]:
    """Prefer the child process; fall back in-process if it cannot be used."""
    if wav_path:
        from .diarize_worker import DiarizationFailed, diarize_in_process
        try:
            return diarize_in_process(
                wav_path, num_speakers=num_speakers, threshold=threshold,
                num_threads=num_threads, progress=progress, cancelled=cancelled)
        except DiarizationFailed as exc:
            log.warning("子行程講者分離失敗（%s），改用同行程執行", exc)
        except Exception as exc:
            log.warning("無法啟動講者分離子行程（%s），改用同行程執行", exc)

    return diarize(samples, num_speakers=num_speakers, threshold=threshold,
                   num_threads=num_threads, progress=progress, cancelled=cancelled)


# --------------------------------------------------------------------------
# Stage 2 - where the speech is
# --------------------------------------------------------------------------
def vad_chunks(samples: np.ndarray, threshold: float = 0.5) -> list[tuple[float, float]]:
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(C.VAD_MODEL)
    cfg.silero_vad.threshold = threshold
    cfg.silero_vad.min_silence_duration = C.VAD_MIN_SILENCE_SEC
    cfg.silero_vad.min_speech_duration = C.VAD_MIN_SPEECH_SEC
    cfg.silero_vad.max_speech_duration = C.VAD_MAX_SPEECH_SEC
    cfg.sample_rate = SAMPLE_RATE
    # CPU, measured: 3.76s vs 37.19s on CUDA for a 5-minute file. Different
    # reason from diarization - the shape here is fixed (512 samples) but each
    # call is only 32 ms of audio through a 640 KB model, so kernel launch and
    # host<->device transfer dwarf the arithmetic. ~9,400 calls per 5 minutes.
    cfg.provider, _ = resolve_provider(C.DIARIZE_PROVIDER)

    total_sec = len(samples) / SAMPLE_RATE
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(30.0, total_sec + 5))

    window = 512
    out: list[tuple[float, float]] = []

    def drain() -> None:
        while not vad.empty():
            seg = vad.front
            out.append((seg.start / SAMPLE_RATE,
                        (seg.start + len(seg.samples)) / SAMPLE_RATE))
            vad.pop()

    for i in range(0, len(samples), window):
        vad.accept_waveform(samples[i:i + window])
        drain()
    vad.flush()
    drain()
    return out


# --------------------------------------------------------------------------
# Stage 3 - intersect the two into speaker-pure units
# --------------------------------------------------------------------------
def _speaker_at(turns: list[tuple[float, float, int]], start: float, end: float) -> int | None:
    """Speaker whose turn overlaps [start, end] the most.

    Falls back to the nearest turn in time when nothing overlaps. Returning a
    sentinel instead put those pieces under a speaker id of -1, which then
    became a real person in the transcript - short interjections like "Yeah"
    that the VAD caught but diarization did not cover were all attributed to a
    phantom extra speaker.
    """
    best, best_overlap = None, 0.0
    for t_start, t_end, spk in turns:
        overlap = min(end, t_end) - max(start, t_start)
        if overlap > best_overlap:
            best, best_overlap = spk, overlap
    if best is not None:
        return best

    nearest, nearest_gap = None, float("inf")
    for t_start, t_end, spk in turns:
        gap = t_start - end if t_start > end else start - t_end
        if 0 <= gap < nearest_gap:
            nearest, nearest_gap = spk, gap
    return nearest


def split_by_speaker(chunks: list[tuple[float, float]],
                     turns: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """Cut each VAD chunk wherever the speaker changes, then re-merge same-speaker runs."""
    edges = sorted({t for turn in turns for t in turn[:2]})
    units: list[tuple[float, float, int]] = []

    for c_start, c_end in chunks:
        inner = [e for e in edges if c_start < e < c_end]
        points = [c_start, *inner, c_end]
        for a, b in zip(points, points[1:]):
            if b - a < 0.05:
                continue
            spk = _speaker_at(turns, a, b)
            units.append((a, b, -1 if spk is None else spk))

    # Fragments produced by consecutive same-speaker turns are one utterance.
    merged: list[list[float | int]] = []
    for start, end, spk in units:
        if merged and merged[-1][2] == spk and start - merged[-1][1] < 0.06:
            merged[-1][1] = end
        else:
            merged.append([start, end, spk])

    # Safety net: speech pyannote found but the VAD missed entirely.
    # Only the parts that are actually uncovered get added. Appending the whole
    # turn duplicated audio already transcribed, producing overlapping segments
    # whose text repeated the sentence before it.
    covered = sorted((m[0], m[1]) for m in merged)
    for t_start, t_end, spk in turns:
        cursor = t_start
        for c_start, c_end in covered:
            if c_end <= cursor:
                continue
            if c_start >= t_end:
                break
            if c_start > cursor:
                gap = min(c_start, t_end) - cursor
                if gap > 0.8:
                    merged.append([cursor, min(c_start, t_end), spk])
            cursor = max(cursor, c_end)
            if cursor >= t_end:
                break
        if t_end - cursor > 0.8:
            merged.append([cursor, t_end, spk])

    merged.sort(key=lambda m: m[0])
    out: list[tuple[float, float, int]] = []
    for m in merged:
        start, end, spk = float(m[0]), float(m[1]), int(m[2])
        if end - start < C.MIN_SEGMENT_SEC:
            continue
        # The VAD caps a chunk at VAD_MAX_SPEECH_SEC, but the safety net above
        # bypasses the VAD entirely and can emit a turn of any length. A 41
        # second unit reached the recogniser that way and crashed the graph
        # outright; models are trained on far shorter spans, so even when one
        # survives it decodes badly. Cut anything oversized into equal pieces.
        span = end - start
        if span > C.VAD_MAX_SPEECH_SEC:
            pieces = int(span // C.VAD_MAX_SPEECH_SEC) + 1
            step = span / pieces
            for i in range(pieces):
                out.append((start + i * step, start + (i + 1) * step, spk))
        else:
            out.append((start, end, spk))
    return out


# --------------------------------------------------------------------------
# Stage 5 - use the words to tidy up who said them
# --------------------------------------------------------------------------
# A backchannel ("嗯", "對", "Yeah") is real speech but far too short and
# generic for the voiceprint to place. The transcript says more about it than
# the audio does, so these rules run after recognition rather than before it.
SHORT_UTTERANCE_SEC = 1.5
CONTINUATION_GAP_SEC = 0.6


def _tidy_speakers(utterances: list[Utterance]) -> list[Utterance]:
    """Fix speaker labels that the text plainly contradicts.

    Two rules, both conservative:

    1. A short interjection sandwiched between two turns of the same speaker is
       almost certainly that speaker, not a third party appearing for 0.4s.
    2. A sentence that ran on without terminal punctuation, immediately
       followed by more speech, is the same person still talking.
    """
    if len(utterances) < 3:
        return utterances

    fixed = list(utterances)
    smoothed = continued = 0

    for i in range(1, len(fixed) - 1):
        current, before, after = fixed[i], fixed[i - 1], fixed[i + 1]
        if before.speaker != after.speaker or current.speaker == before.speaker:
            continue
        short = (current.end - current.start) <= SHORT_UTTERANCE_SEC
        if short or is_backchannel(current.text):
            fixed[i] = Utterance(current.start, current.end, before.speaker,
                                 current.text)
            smoothed += 1

    for i in range(1, len(fixed)):
        current, previous = fixed[i], fixed[i - 1]
        if current.speaker == previous.speaker:
            continue
        gap = current.start - previous.end
        if 0 <= gap <= CONTINUATION_GAP_SEC and looks_unfinished(previous.text)                 and not is_backchannel(current.text):
            fixed[i] = Utterance(current.start, current.end, previous.speaker,
                                 current.text)
            continued += 1

    if smoothed or continued:
        log.info("  依文字修正講者：短插話 %d 句、接續語句 %d 句", smoothed, continued)
    return fixed


# --------------------------------------------------------------------------
# Stage 4 - recognise each unit
# --------------------------------------------------------------------------
def transcribe(samples: np.ndarray, model_key: str, *, num_speakers: int = -1,
               cluster_threshold: float = 0.5, language: str = "",
               zh_mode: str = DEFAULT_ZH_MODE,
               num_threads: int = 4, progress: ProgressFn | None = None,
               cancelled: Callable[[], bool] | None = None,
               wav_path: str | None = None) -> list[Utterance]:
    def report(stage: str, frac: float, detail: str = "") -> None:
        if progress:
            progress(stage, frac, detail)

    def stop() -> bool:
        return bool(cancelled and cancelled())

    t_start = time.time()
    audio_sec = len(samples) / SAMPLE_RATE
    log.info("開始辨識：%.0f 秒音檔，模型 %s，%d 執行緒，講者數 %s",
             audio_sec, model_key, num_threads,
             num_speakers if num_speakers > 0 else "自動")

    label = ASR_MODELS[model_key]["label"]

    t0 = time.time()
    report("load", 0.0, f"{label}（第一次用這個模型要多等幾秒）")
    borrowed = use_recognizer(model_key, num_threads=num_threads, language=language)
    recognizer = borrowed.__enter__()
    load_sec = time.time() - t0
    log.info("  模型載入完成（%.1f 秒）", load_sec)
    report("load", 1.0, f"已載入（{load_sec:.0f} 秒）")
    if stop():
        return []

    t0 = time.time()
    report("diarize", 0.0, "載入講者模型…")
    # Diarization runs in a child process when the audio is on disk: its
    # process() call holds the GIL for the whole run, which froze the web UI
    # for seconds at a time. See app/diarize_worker.py.
    turns = _diarize_maybe_out_of_process(
        samples, wav_path, num_speakers=num_speakers, threshold=cluster_threshold,
        num_threads=num_threads, progress=progress, cancelled=cancelled)
    speakers_found = len({t[2] for t in turns})
    log.info("  講者分析完成：%d 位講者、%d 個發言段（%.1f 秒）",
             speakers_found, len(turns), time.time() - t0)
    report("diarize", 1.0,
           f"找到 {speakers_found} 位講者、{len(turns)} 段發言")
    if stop():
        return []

    t0 = time.time()
    report("vad", 0.0, "找出每一句的開始與結束…")
    chunks = vad_chunks(samples)
    units = split_by_speaker(chunks, turns)
    log.info("  語句切分完成：%d 句（%.1f 秒）", len(units), time.time() - t0)
    report("vad", 1.0, f"切出 {len(units)} 句")
    if stop():
        return []

    # Sentences are decoded in batches. decode_streams() runs a whole batch
    # through the graph at once, which measured ~1.8x faster than decoding one
    # at a time and produces byte-identical text. Raising num_threads instead
    # does not help here - the per-segment audio is too short for intra-op
    # parallelism to pay off, and 20 threads measured slower than 6.
    t_asr = time.time()
    results: list[Utterance] = []
    decoded = 0
    failed_segments: list[tuple[float, float]] = []
    min_samples = int(0.1 * SAMPLE_RATE)

    usable = [(s, e, spk) for s, e, spk in units
              if int(e * SAMPLE_RATE) - int(s * SAMPLE_RATE) >= min_samples]

    for offset in range(0, len(usable), C.ASR_BATCH):
        if stop():
            break
        batch = usable[offset:offset + C.ASR_BATCH]
        batch_len = len(batch)
        # Announce the batch before decoding it, so the label moves twice per
        # batch instead of freezing until the whole batch comes back.
        report("asr", offset / len(usable),
               f"辨識第 {offset + 1}–{offset + batch_len} / {len(usable)} 句")

        streams = []
        for start, end, _ in batch:
            stream = recognizer.create_stream()
            stream.accept_waveform(
                SAMPLE_RATE, samples[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)])
            streams.append(stream)

        # A single awkward segment must not cost the whole meeting. Some models
        # fail inside the graph on very short inputs - x-asr-zipformer-punct
        # raises "Reshape ... Input shape:{1,41,16}" on a sub-second clip - and
        # that used to kill the run after tens of minutes of work. Retry the
        # batch one at a time and drop only what genuinely cannot be decoded.
        try:
            recognizer.decode_streams(streams)
        except Exception as exc:
            log.warning("批次辨識失敗（%s），改為逐句處理這一批", exc)
            survivors, kept = [], []
            for (start, end, spk), stream in zip(batch, streams):
                try:
                    recognizer.decode_stream(stream)
                except Exception as inner:
                    log.warning("  跳過 %.1f-%.1f 秒：%s", start, end, inner)
                    failed_segments.append((start, end))
                    continue
                survivors.append(stream)
                kept.append((start, end, spk))
            batch, streams = kept, survivors
        decoded += len(streams)

        for (start, end, spk), stream in zip(batch, streams):
            text = clean(stream.result.text or "", zh_mode)
            # A line that is only punctuation or a single stray character is a
            # mis-decoded noise burst. Keeping them polluted the transcript and,
            # worse, each one could become its own speaker.
            if text and is_meaningful(text):
                results.append(Utterance(round(start, 3), round(end, 3), spk, text))

        done = min(offset + batch_len, len(usable))
        report("asr", done / len(usable), f"第 {done} / {len(usable)} 句")

    borrowed.__exit__(None, None, None)
    results = _tidy_speakers(results)

    # Fix the words the recogniser reliably gets wrong (names, jargon).
    from . import vocabulary
    rules = vocabulary.rules_for()
    if rules:
        fixed = 0
        for i, utterance in enumerate(results):
            text, n = vocabulary.apply(utterance.text, rules)
            if n:
                results[i] = Utterance(utterance.start, utterance.end,
                                       utterance.speaker, text)
                fixed += n
        if fixed:
            log.info("  詞語修正：%d 處", fixed)

    # Restore punctuation for models that emit none. Optional add-on: without
    # it downloaded this is a no-op and the transcript is unchanged.
    from . import punctuation
    if punctuation.should_apply(model_key):
        report("asr", 0.99, "加上標點")
        n = 0
        for i, u in enumerate(results):
            text = punctuation.restore_if_needed(u.text)
            if text != u.text:
                results[i] = Utterance(u.start, u.end, u.speaker, text)
                n += 1
        if n:
            log.info("  標點還原：%d 句", n)

    log.info("  語音辨識完成：%d 句有內容（%.1f 秒），總計 %.1f 秒",
             len(results), time.time() - t_asr, time.time() - t_start)
    if failed_segments:
        # Say so rather than letting the gaps look like silence.
        log.warning("  有 %d 句無法辨識已略過（模型在這些片段上出錯）：%s",
                    len(failed_segments),
                    "、".join(f"{s:.1f}-{e:.1f}s" for s, e in failed_segments[:8]))

    # Decoding many segments and getting nothing back is a misconfigured model,
    # not a quiet recording. Fail loudly instead of saving an empty transcript.
    if decoded >= 5 and not results:
        raise RuntimeError(
            f"{ASR_MODELS[model_key]['label']} 對 {decoded} 個語音片段都沒有輸出任何文字。"
            f"通常是模型設定不對（例如語言沒設定），請換個模型或改語言設定後重試。"
            f"伺服器視窗可能有更詳細的訊息。"
        )
    return results
