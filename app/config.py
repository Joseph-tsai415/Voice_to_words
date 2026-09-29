"""Paths, tunables, and the ASR model catalogue."""
from __future__ import annotations

import os
from pathlib import Path


def _clamp_env(name: str, default: int, low: int, high: int) -> int:
    """Read an int from the environment, clamped to a sane range."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
HUB_DIR = MODELS_DIR / "hub"          # downloaded Hugging Face models land here
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"

# Shared (non-ASR) models. These ship with the repo.
VAD_MODEL = MODELS_DIR / "silero_vad.onnx"
SEGMENTATION_MODEL = MODELS_DIR / "segmentation" / "model.onnx"
SPEAKER_EMBEDDING_MODEL = MODELS_DIR / "speaker-embedding.onnx"

SAMPLE_RATE = 16000

# --- CPU budget ------------------------------------------------------------
# Two dials interact: how many meetings run at once, and how many threads each
# one gets. Their product is the number of cores in use, so they are sized
# together from the actual core count rather than hard-coded.
#
# Throughput favours more concurrent jobs over more threads per job: ONNX
# intra-op parallelism gives diminishing returns past ~6 threads on the short
# segments this pipeline feeds it, while a second job keeps its own cores busy.
# Both are overridable: SCRIBE_JOBS and SCRIBE_THREADS.
CPU_COUNT = os.cpu_count() or 4
_BUDGET = max(2, CPU_COUNT - 2)          # leave a couple of cores for the OS/UI

# Measured on 20 cores, four 3-minute recordings, GPU recognition + CPU
# diarization. Throughput (higher is better):
#     1 job  x 6 threads   104.0s   6.9x realtime
#     2 jobs x 6 threads    78.0s   9.2x        <- best
#     3 jobs x 6 threads    82.1s   8.8x        <- default
#     4 jobs x 6 threads    81.3s   8.9x
#     2 jobs x 10 threads  128.9s   5.6x        <- more threads hurts
# Two to four concurrent jobs are within ~5% of each other; one is clearly
# worse. Diarization simply stops scaling past ~6 threads, so extra cores are
# only reachable by running more jobs, not bigger ones.
DEFAULT_THREADS = _clamp_env("SCRIBE_THREADS", min(6, max(2, _BUDGET // 3)), 1, CPU_COUNT)
MAX_CONCURRENT_JOBS = _clamp_env("SCRIBE_JOBS", max(1, _BUDGET // DEFAULT_THREADS), 1, 8)

# How many sentences go through the recogniser per decode_streams() call.
# Measured on a 6-minute recording: batch 16 ran 1.8x faster than one-at-a-time
# with identical text. Larger batches raise peak memory for little extra gain.
ASR_BATCH = _clamp_env("SCRIBE_BATCH", 16, 1, 128)

# Execution provider: auto | cpu | cuda. "auto" uses the GPU when the card, the
# driver and a working CUDA build are all present, and CPU otherwise.
PROVIDER = (os.environ.get("SCRIBE_PROVIDER", "auto") or "auto").strip().lower()

# Diarization is deliberately kept on the CPU. Measured on an RTX 3070 Ti with a
# 5-minute recording: recognition 12.6s -> 0.9s on the GPU (14x faster), but
# diarization 47.9s -> 92.6s.
#
# The cause is input *shape changes*, not clip length or call count. Replaying
# the same 62 embedding calls with identical-length inputs, the GPU was 4.3x
# FASTER (0.88s vs 3.76s); with five lengths rotating it was already 1.9x
# slower, and with every length different, 2.8x slower. ONNX Runtime's CUDA
# provider re-plans on each new shape (cuDNN also searches for a convolution
# algorithm per shape), and diarization hands it a different-length speech
# region every single call. Splitting it further: embedding on GPU costs +30.1s,
# segmentation +4.3s.
#
# sherpa-onnx exposes CudaConfig.cudnn_conv_algo_search, which would blunt this,
# but only OnlineModelConfig accepts a provider_config - the embedding extractor
# and the offline recognisers take a bare provider string, so the knob cannot be
# reached from here.
#
# Override with SCRIBE_DIARIZE_PROVIDER if a different card behaves differently.
DIARIZE_PROVIDER = (os.environ.get("SCRIBE_DIARIZE_PROVIDER", "cpu")
                    or "cpu").strip().lower()

# Thread choices offered in the UI, capped at the real core count.
THREAD_CHOICES = sorted({2, 4, DEFAULT_THREADS, 8, 12, 16, CPU_COUNT} & set(range(1, CPU_COUNT + 1)))

# --- Segmentation tunables -------------------------------------------------
# A speech chunk shorter than this is dropped rather than sent to the ASR.
MIN_SEGMENT_SEC = 0.20
# Consecutive same-speaker segments closer than this are drawn as one bubble.
BUBBLE_GAP_SEC = 1.20
# Silence that ends a VAD chunk / longest chunk before a forced cut.
VAD_MIN_SILENCE_SEC = 0.35
VAD_MIN_SPEECH_SEC = 0.20
VAD_MAX_SPEECH_SEC = 20.0
# Longest sentence left as one segment before it is cut at a clause boundary.
# A VAD chunk is however much someone said between two pauses, so it is often
# a paragraph: on a real 25-minute meeting, 41 of 118 segments ran past 80
# characters and the longest was 357. A segment is the unit the user edits and
# reassigns, so an unreadable one defeats the point of per-sentence attribution.
SENTENCE_MAX_CHARS = 80

# --- ASR catalogue ---------------------------------------------------------
# Every entry runs on CPU through sherpa-onnx. `files` maps a logical role to a
# path inside the model directory; `repo` is the Hugging Face repo the files are
# pulled from. `kind` selects the sherpa-onnx factory in asr.py.
ASR_MODELS: dict[str, dict] = {
    "sense-voice": {
        "label": "SenseVoice (內建)",
        "kind": "sense_voice",
        "repo": None,
        "languages": "中 / 英 / 日 / 韓 / 粵",
        "size_mb": 900,
        "note": "隨專案附帶，免下載。輸出簡體，可用「中文轉換」轉繁體。",
        "upstream": "FunAudioLLM/SenseVoiceSmall",
        "arch": "SenseVoice encoder（非自迴歸）",
        "dir": MODELS_DIR / "sense-voice",
        "files": {"model": "model.onnx", "tokens": "tokens.txt"},
        # Default to Chinese, not auto-detect. On a real Chinese meeting the
        # auto path transcribed filler sounds as Japanese kana ("嗯嗯" ->
        # "うんううん"); pinning the language removed every kana character and
        # produced more Chinese, measured on the same 4 minutes of audio.
        # Mixed-in English words still come through.
        "language_codes": ["zh", "en", "ja", "ko", "yue", "auto"],
        "default_language": "zh",
        "punctuates": True,
    },
    "x-asr-zipformer-punct": {
        "label": "X-ASR Zipformer 中英 (含標點)",
        "kind": "transducer",
        "repo": "csukuangfj2/sherpa-onnx-x-asr-zipformer-transducer-zh-en-punct-int8-2026-06-03",
        "languages": "中 / 英",
        "size_mb": 176,
        "note": "2026-06 釋出。輸出自帶標點與繁體。",
        "upstream": None,   # 上游是 GitHub Gilgamesh-J/X-ASR，不在排行榜上
        "arch": "Zipformer transducer",
        "punctuates": True,
        "files": {
            "encoder": "encoder-epoch-99-avg-1.int8.onnx",
            "decoder": "decoder-epoch-99-avg-1.onnx",
            "joiner": "joiner-epoch-99-avg-1.int8.onnx",
            "tokens": "tokens.txt",
        },
    },
    "fire-red-asr2-ctc": {
        "label": "FireRedASR2-CTC 中英",
        "kind": "fire_red_asr_ctc",
        "repo": "csukuangfj2/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25",
        "languages": "中 / 英",
        "size_mb": 740,
        "note": "FireRedASR2-AED 的 CTC 分支。",
        "upstream": "FireRedTeam/FireRedASR2-AED",
        "arch": "CTC",
        "files": {"model": "model.int8.onnx", "tokens": "tokens.txt"},
    },
    "fire-red-asr2": {
        "label": "FireRedASR2-AED 中英",
        "kind": "fire_red_asr",
        "repo": "csukuangfj2/sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26",
        "languages": "中 / 英",
        "size_mb": 1180,
        "note": "FireRedASR2-AED 完整版。",
        "upstream": "FireRedTeam/FireRedASR2-AED",
        "arch": "Attention encoder-decoder",
        "files": {
            "encoder": "encoder.int8.onnx",
            "decoder": "decoder.int8.onnx",
            "tokens": "tokens.txt",
        },
    },
    "qwen3-asr": {
        "label": "Qwen3-ASR 0.6B (多語 + 方言)",
        "kind": "qwen3",
        "repo": "csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        "languages": "30 語 + 22 種中文方言",
        "size_mb": 940,
        "note": "涵蓋 30 種語言與 22 種中文方言。",
        "upstream": "Qwen/Qwen3-ASR-0.6B-hf",
        "arch": "Qwen3 speech LLM",
        "files": {
            "conv_frontend": "conv_frontend.onnx",
            "encoder": "encoder.int8.onnx",
            "decoder": "decoder.int8.onnx",
            "tokenizer": "tokenizer",
        },
        "extra_files": [
            "tokenizer/merges.txt",
            "tokenizer/tokenizer_config.json",
            "tokenizer/vocab.json",
        ],
    },
    "cohere-transcribe": {
        "label": "Cohere Transcribe 14 語",
        "kind": "cohere",
        "repo": "csukuangfj2/sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01",
        "languages": "14 語 (含中/英)",
        "size_mb": 2840,
        "note": "14 種語言。必須指定辨識語言。",
        "upstream": "CohereLabs/cohere-transcribe-03-2026",
        "arch": "Encoder-decoder",
        "files": {
            "encoder": "encoder.int8.onnx",
            "decoder": "decoder.int8.onnx",
            "tokens": "tokens.txt",
        },
        "extra_files": ["encoder.int8.onnx.data"],
        # Required: sherpa-onnx aborts every stream with "Please specify a
        # language for Cohere Transcribe" and returns empty text otherwise.
        "language_codes": ["zh", "en", "ja", "ko", "ar", "de", "el", "es",
                           "fr", "it", "nl", "pl", "pt", "vi"],
        "default_language": "zh",
        "language_required": True,
    },
    "paraformer-zh": {
        "label": "Paraformer 中文",
        "kind": "paraformer",
        "repo": "csukuangfj/sherpa-onnx-paraformer-zh-int8-2025-10-07",
        "languages": "中 / 英",
        "size_mb": 238,
        "note": "非自迴歸中文模型。",
        "upstream": "funasr/paraformer-zh",
        "arch": "Paraformer",
        "files": {"model": "model.int8.onnx", "tokens": "tokens.txt"},
    },
    # Not a recogniser. Cohere Transcribe and the FireRedASR2 models return an
    # unbroken wall of characters - a 7,800-character transcript came back with
    # 43 punctuation marks - which is exactly what makes a verbatim transcript
    # hard to read. This restores sentence breaks afterwards, so the choice of
    # recogniser stops being a choice about punctuation. `role` keeps it out of
    # the recogniser picker while still letting it download like anything else.
    "punct-ct-transformer": {
        "label": "標點還原（中英）",
        "role": "punct",
        "kind": "ct_transformer",
        "repo": "csukuangfj/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12",
        "languages": "中 / 英",
        "size_mb": 281,
        "note": "附加元件，不是辨識模型。替沒有標點的模型補上逗號、句號、問號。",
        "upstream": "alibaba-damo/punc_ct-transformer_cn-en",
        "arch": "CT-Transformer",
        "files": {"model": "model.onnx"},
        "extra_files": ["tokens.json"],
    },
}

DEFAULT_MODEL = "sense-voice"


def recognizer_keys() -> list[str]:
    """Catalogue keys that can actually transcribe - excludes add-ons."""
    return [k for k, spec in ASR_MODELS.items() if spec.get("role", "asr") == "asr"]

# --- Languages -------------------------------------------------------------
# Some recognizers take a language hint; Cohere Transcribe *requires* one and
# silently decodes to nothing without it. Codes are the ones sherpa-onnx accepts.
LANGUAGE_LABELS = {
    "auto": "自動偵測",
    "zh": "中文", "en": "英文", "ja": "日文", "ko": "韓文", "yue": "粵語",
    "ar": "阿拉伯文", "de": "德文", "el": "希臘文", "es": "西班牙文",
    "fr": "法文", "it": "義大利文", "nl": "荷蘭文", "pl": "波蘭文",
    "pt": "葡萄牙文", "vi": "越南文",
}


def language_options(key: str) -> list[dict]:
    """Language choices for a model, as [{code, label}]. Empty = takes none."""
    codes = ASR_MODELS.get(key, {}).get("language_codes") or []
    return [{"code": c, "label": LANGUAGE_LABELS.get(c, c)} for c in codes]


def resolve_language(key: str, requested: str) -> str:
    """Pick a valid language for this model, never leaving a required one blank.

    An empty request means "not specified" and falls back to the model's
    default. Auto-detection has to be asked for explicitly with "auto", because
    on a Chinese meeting it transcribed filler sounds as Japanese kana - not
    something to land in by accident.
    """
    spec = ASR_MODELS.get(key, {})
    codes = spec.get("language_codes") or []
    if not codes:
        return ""
    requested = (requested or "").strip()
    if requested == "auto":
        return ""                       # what sherpa-onnx expects for auto
    if requested and requested in codes:
        return requested
    return spec.get("default_language", codes[0])

# Colours assigned to speakers in the UI, in order.
SPEAKER_COLORS = [
    "#2563eb", "#db2777", "#059669", "#d97706", "#7c3aed",
    "#0891b2", "#dc2626", "#65a30d", "#c026d3", "#0284c7",
]


def model_dir(key: str) -> Path:
    """Directory holding a model's files."""
    spec = ASR_MODELS[key]
    return spec.get("dir") or (HUB_DIR / key)


def model_paths(key: str) -> dict[str, Path]:
    """Absolute path for each of a model's logical file roles.

    A role may name a directory (qwen3's tokenizer/), so these are what the
    sherpa-onnx factory receives - not what has to exist on disk. Use
    required_files() for completeness checks.
    """
    base = model_dir(key)
    return {role: base / rel for role, rel in ASR_MODELS[key]["files"].items()}


def required_files(key: str) -> list[str]:
    """Every concrete file that must be on disk, repo-relative.

    Roles that name a directory are dropped - the files inside them are listed
    under extra_files. extra_files also covers files needed on disk but never
    passed as an argument, such as an ONNX external-data blob (cohere's 2.7 GB
    encoder.int8.onnx.data). Leaving those out of the check makes a model report
    itself ready while it is still downloading.
    """
    spec = ASR_MODELS[key]
    roles = [rel for rel in spec["files"].values() if Path(rel).suffix]
    return roles + list(spec.get("extra_files", []))


def missing_files(key: str) -> list[str]:
    """Required files that are absent or zero-length, repo-relative."""
    base = model_dir(key)
    out = []
    for rel in required_files(key):
        path = base / rel
        try:
            if not path.is_file() or path.stat().st_size == 0:
                out.append(rel)
        except OSError:
            out.append(rel)
    return out


def is_downloaded(key: str) -> bool:
    return not missing_files(key)
