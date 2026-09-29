"""Package init.

Its one job is to make the CUDA libraries findable before anything imports
sherpa-onnx. The NVIDIA pip wheels drop their DLLs under
site-packages/nvidia/*/bin, and on Windows Python 3.8+ deliberately ignores PATH
when loading extension modules - only directories registered with
os.add_dll_directory() are searched. Without this, a perfectly good CUDA build
fails with "Failed to load shared library" and silently runs on the CPU.

Costs nothing when the NVIDIA packages are not installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_dll_handles = []          # keep the handles alive for the process lifetime


def _register_cuda_dlls() -> list[str]:
    """Put every bundled NVIDIA bin/ directory on the DLL search path.

    PATH is the one that matters. onnxruntime loads
    onnxruntime_providers_cuda.dll itself, and that DLL's own dependencies are
    resolved by the plain Windows loader, which reads PATH. Registering the
    directories with os.add_dll_directory() alone was measured to fail -
    "Failed to load shared library" - while adding them to PATH succeeded. Both
    are done here; the add_dll_directory call covers anything Python loads.
    """
    if os.name != "nt":
        return []                      # Linux resolves these via RPATH/LD_LIBRARY_PATH

    added: list[str] = []
    for site_dir in {Path(p) for p in sys.path if p}:
        nvidia = site_dir / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in sorted(nvidia.glob("*/bin")):
            if bin_dir.is_dir():
                added.append(str(bin_dir))
        if added:
            break                      # one site-packages tree is enough

    if not added:
        return []

    for bin_dir in added:
        try:
            _dll_handles.append(os.add_dll_directory(bin_dir))
        except (OSError, AttributeError):
            pass

    existing = os.environ.get("PATH", "")
    missing = [d for d in added if d.lower() not in existing.lower()]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + existing
    return added


CUDA_DLL_DIRS = _register_cuda_dlls()
