"""Which ONNX execution provider to run on, and whether a GPU is usable.

Three separate things have to line up before a GPU does any work:

1. The NVIDIA card is enabled and has a driver (`nvidia-smi` answers).
2. sherpa-onnx was installed from the CUDA wheel, not the default CPU one.
3. The CUDA runtime and cuDNN the wheel was built against are present.

If any of those is missing, sherpa-onnx logs "Fallback to cpu!" and carries on -
correct behaviour, but silent, so this module reports the state plainly instead
of leaving the user wondering why nothing got faster.
"""
from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("scribe.gpu")

# auto = use the GPU when everything lines up, otherwise CPU.
VALID_PROVIDERS = ("auto", "cpu", "cuda")

CUDA_RUNTIME_HINT = (
    "已安裝 GPU 版套件，但缺少 CUDA 12 執行環境或 cuDNN 9。"
    "下載： https://developer.nvidia.com/cuda-downloads"
    " 與 https://developer.nvidia.com/cudnn-downloads"
)

CUDA_WHEEL_HINT = (
    'pip install --force-reinstall "sherpa-onnx==1.13.6+cuda12.cudnn9" '
    "--no-index -f https://k2-fsa.github.io/sherpa/onnx/cuda.html"
)


@functools.lru_cache(maxsize=1)
def nvidia_gpu() -> dict | None:
    """The first NVIDIA GPU nvidia-smi reports, or None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    first = out.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 3:
        return None
    try:
        vram_mb = int(float(parts[1]))
    except ValueError:
        vram_mb = 0
    return {"name": parts[0], "vram_mb": vram_mb, "driver": parts[2]}


@functools.lru_cache(maxsize=1)
def sherpa_version() -> str:
    """Installed sherpa-onnx version, including the +cuda local tag if present."""
    try:
        import importlib.metadata as md
        return md.version("sherpa-onnx")
    except Exception:
        pass
    try:
        import sherpa_onnx
        return str(getattr(sherpa_onnx, "__version__", ""))
    except Exception:
        return ""


def sherpa_has_cuda() -> bool:
    """Is the installed wheel a CUDA build? Says nothing about it working."""
    return "cuda" in sherpa_version().lower()


# The message sherpa-onnx prints when it wanted a GPU and could not have one.
_FALLBACK = "Fallback to cpu"

_PROBE = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.config import VAD_MODEL
import sherpa_onnx
cfg = sherpa_onnx.VadModelConfig()
cfg.silero_vad.model = str(VAD_MODEL)
cfg.sample_rate = 16000
cfg.provider = "cuda"
sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=5)
print("BUILT", flush=True)
"""


@functools.lru_cache(maxsize=1)
def cuda_works() -> tuple[bool, str]:
    """Actually try CUDA and see whether it engaged.

    Installing the CUDA wheel is not enough - it still needs the CUDA runtime
    and cuDNN beside it. Without them onnxruntime quietly logs "Fallback to
    cpu!" and keeps going, so the only trustworthy answer is to load a model
    and look. A child process is used so a hard CUDA failure cannot take the
    server down with it.
    """
    if not sherpa_has_cuda():
        return False, f"安裝的是 CPU 版 sherpa-onnx（{sherpa_version() or '未知版本'}）"

    root = str(Path(__file__).resolve().parent.parent)
    try:
        out = subprocess.run(
            [sys.executable, "-c", _PROBE, root],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, cwd=root,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"CUDA 測試無法執行：{exc}"

    combined = (out.stdout or "") + (out.stderr or "")
    if _FALLBACK.lower() in combined.lower():
        return False, "CUDA 無法初始化（缺少 CUDA 12 執行環境或 cuDNN 9），已退回 CPU"
    if out.returncode != 0 or "BUILT" not in combined:
        detail = (out.stderr or "").strip().splitlines()
        return False, f"CUDA 測試失敗：{detail[-1][:120] if detail else '未知錯誤'}"
    return True, ""


def vram_mb() -> tuple[int, int] | None:
    """(total, free) video memory in MB, or None if there is no GPU.

    Unlike system RAM there is no page file behind this. Overcommitting it does
    not get slow, it fails outright with "CUDA failure 2: out of memory", so
    the model cache has to respect it.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        total, free = out.stdout.strip().splitlines()[0].split(",")
        return int(float(total)), int(float(free))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def resolve(requested: str = "auto") -> tuple[str, str]:
    """(provider to use, human-readable reason)."""
    requested = (requested or "auto").lower()
    if requested not in VALID_PROVIDERS:
        requested = "auto"

    if requested == "cpu":
        return "cpu", "設定為只用 CPU"

    gpu = nvidia_gpu()
    if not gpu:
        return "cpu", "找不到可用的 NVIDIA GPU（顯示卡可能被停用或未安裝驅動）"

    # Installing the CUDA wheel is not proof it runs - check that it really does.
    works, why_not = cuda_works()
    if works:
        return "cuda", f"使用 GPU：{gpu['name']}（{gpu['vram_mb'] // 1024} GB）"
    return "cpu", f"偵測到 {gpu['name']}，但 {why_not}"


def status(requested: str = "auto") -> dict:
    """Everything the UI needs to explain the current state."""
    gpu = nvidia_gpu()
    provider, reason = resolve(requested)
    has_build = sherpa_has_cuda()
    hint = None
    if provider != "cuda" and gpu:
        hint = CUDA_RUNTIME_HINT if has_build else CUDA_WHEEL_HINT
    return {
        "requested": requested,
        "provider": provider,
        "reason": reason,
        "gpu": gpu,
        "sherpa_version": sherpa_version(),
        "sherpa_cuda_build": has_build,
        "install_hint": hint,
    }


def refresh() -> None:
    """Forget cached probes - the card or the wheel may have just changed."""
    nvidia_gpu.cache_clear()
    sherpa_version.cache_clear()
    cuda_works.cache_clear()
