"""Load any common audio file as 16 kHz mono float32, which every model expects.

WAV/FLAC/OGG/MP3 go through libsndfile; everything else (m4a/aac, mp4, mov,
wma, opus, webm, amr, alac...) is decoded by PyAV, which bundles the FFmpeg
libraries so no external ffmpeg binary is required.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from .config import SAMPLE_RATE

# Decoded by libsndfile directly. Everything else falls to PyAV.
NATIVE_SUFFIXES = {".wav", ".flac", ".ogg", ".oga", ".aiff", ".aif", ".au", ".w64", ".caf"}

# Offered in the upload dialog / accepted by the server.
SUPPORTED_SUFFIXES = sorted(NATIVE_SUFFIXES | {
    ".m4a", ".mp3", ".mp4", ".m4b", ".aac", ".alac", ".mov", ".wma", ".asf",
    ".opus", ".webm", ".mkv", ".amr", ".3gp", ".avi", ".ts", ".mpga",
})


def upload_name_and_suffix(filename: str) -> tuple[str, str]:
    """Split an uploaded filename into (display name, lower-case suffix).

    Deliberately does **not** use secure_filename(): that strips every
    non-ASCII character, so a Chinese recording name collapses to its own
    extension -- `secure_filename("孟青宴安討論.m4a")` is `"m4a"`. The suffix
    then came out empty, which silently skipped the supported-format check
    (it only fires when a suffix is present), left the upload stored as
    `source` with no extension, and named the project "m4a" whenever the
    dialog sends no explicit name -- which it does for every multi-file
    upload.

    Sanitising was never buying anything here. The upload is written to
    `source<suffix>` inside a uuid-named folder, so the user's filename never
    reaches the filesystem; only the suffix does, and the caller checks that
    against SUPPORTED_SUFFIXES. Path separators are still stripped from both
    halves so neither can climb out of the project folder.
    """
    raw = (filename or "").replace("\\", "/").split("/")[-1].strip()
    stem, dot, ext = raw.rpartition(".")
    if not dot:                       # no extension at all
        stem, ext = raw, ""
    suffix = ("." + ext.lower()) if ext else ""
    if "/" in suffix or "\\" in suffix or ".." in suffix:
        suffix = ""
    return (stem.strip() or "錄音"), suffix


class AudioError(RuntimeError):
    pass


def _decode_with_av(path: Path) -> np.ndarray:
    """Decode + downmix + resample in one pass using FFmpeg's swresample."""
    try:
        import av
        from av.audio.resampler import AudioResampler
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise AudioError(
            f"讀取 {path.suffix} 需要 PyAV。請執行： pip install av"
        ) from exc

    try:
        container = av.open(str(path))
    except Exception as exc:
        raise AudioError(f"無法開啟音檔 {path.name}：{exc}") from exc

    with container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise AudioError(f"{path.name} 裡面沒有音訊軌。")
        stream.thread_type = "AUTO"

        resampler = AudioResampler(format="fltp", layout="mono", rate=SAMPLE_RATE)
        chunks: list[np.ndarray] = []
        try:
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):        # flush the resampler
                chunks.append(out.to_ndarray().reshape(-1))
        except Exception as exc:
            raise AudioError(f"解碼 {path.name} 失敗：{exc}") from exc

    if not chunks:
        raise AudioError(f"{path.name} 沒有解出任何音訊。")
    return np.concatenate(chunks).astype(np.float32, copy=False)


def load_16k_mono(path: str | Path) -> np.ndarray:
    """Return the file as a 1-D float32 array sampled at 16 kHz."""
    path = Path(path)
    if not path.exists():
        raise AudioError(f"找不到音檔：{path}")

    if path.suffix.lower() in NATIVE_SUFFIXES:
        try:
            samples, sr = sf.read(str(path), dtype="float32", always_2d=False)
            samples = np.asarray(samples, dtype=np.float32)
            if samples.ndim > 1:                  # downmix to mono
                samples = samples.mean(axis=1, dtype=np.float32)
            if sr != SAMPLE_RATE:                 # band-limited, not naive decimation
                samples = soxr.resample(samples, sr, SAMPLE_RATE).astype(np.float32)
        except AudioError:
            raise
        except Exception:
            samples = _decode_with_av(path)       # malformed header, odd codec in a .wav
    else:
        samples = _decode_with_av(path)

    if samples.size == 0:
        raise AudioError("音檔沒有任何內容。")
    return np.ascontiguousarray(samples, dtype=np.float32)


def probe_duration(path: str | Path) -> float:
    """Length in seconds from the container header - no full decode.

    Lets a project show its duration the moment it is created, before the
    worker gets round to decoding it.
    """
    path = Path(path)
    if path.suffix.lower() in NATIVE_SUFFIXES:
        try:
            info = sf.info(str(path))
            return float(info.frames) / info.samplerate
        except Exception:
            pass
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration:
                return float(container.duration) / 1_000_000     # AV_TIME_BASE
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is not None and stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except Exception:
        pass
    return 0.0


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")


def duration_of(samples: np.ndarray) -> float:
    return float(len(samples)) / SAMPLE_RATE
