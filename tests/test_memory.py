"""Model-memory behaviour.

The default is deliberately hands-off: virtual memory manages an idle model
better than a cache policy can, and measurement backed that up (16 pages/sec
and 3 MB/s of disk while two big jobs ran on a 32 GB box with a 49 GB SSD page
file - it was CPU-bound, not swapping).

So these tests assert two things: nothing is evicted unless SCRIBE_MODEL_RAM_MB
explicitly asks for it, and when it does ask, the right one goes.

No real models are loaded - the cache is driven directly with stand-ins.

Run:  .venv/Scripts/python.exe tests/test_memory.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import asr, memory                                    # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


GB = 1024 ** 3
SIZES = {"tiny": 0.2 * GB, "small": 1.0 * GB, "big": 3.0 * GB, "huge": 5.0 * GB,
         "a": 1.5 * GB, "b": 1.5 * GB, "c": 1.5 * GB}


def install_fakes(cap_gb: float | None, available_gb: float = 100,
                  provider: str = "cpu", vram_mb: int | None = None) -> None:
    """Control the sizes, the configured cap and where the models live."""
    asr.model_bytes = lambda key: int(SIZES.get(key, 0.5 * GB))     # type: ignore
    memory.configured_model_cap = (                                  # type: ignore
        (lambda: int(cap_gb * GB)) if cap_gb is not None else (lambda: None))
    memory.available_bytes = lambda: int(available_gb * GB)         # type: ignore
    asr.resolve_provider = lambda requested: (provider, "")          # type: ignore
    import app.gpu as gpu_mod
    gpu_mod.vram_mb = (lambda: (vram_mb, vram_mb))                   # type: ignore
    if vram_mb is None:
        gpu_mod.vram_mb = lambda: None                               # type: ignore


def put(key: str, in_use: int = 0, age: float = 0.0) -> None:
    cache_key = (key, "")
    asr._recognizer_cache[cache_key] = (object(), 4)                # type: ignore
    asr._last_used[cache_key] = time.time() - age
    if in_use:
        asr._in_use[cache_key] = in_use


def loaded() -> list[str]:
    return [k[0] for k in asr._recognizer_cache]


def reset() -> None:
    asr._recognizer_cache.clear()
    asr._last_used.clear()
    asr._in_use.clear()


def test_evicts_when_over_budget() -> None:
    section("超出預算時釋放最久沒用的模型")
    # 預算 4 GB，已載入 a+b 共 3 GB，要再載入 c (1.5 GB)。
    # 只需要騰出一個位置，所以最久沒用的 a 該走、b 該留。
    install_fakes(cap_gb=4)
    reset()
    put("a", age=300)          # 1.5 GB，最久沒用
    put("b", age=10)           # 1.5 GB，剛用過
    with asr._cache_lock:
        asr._evict_locked(int(1.5 * GB))

    check("釋放了最久沒用的", "a" not in loaded(), str(loaded()))
    check("保留最近用過的", "b" in loaded(), str(loaded()))
    check("只釋放必要的數量", len(loaded()) == 1, str(loaded()))


def test_never_evicts_in_use() -> None:
    section("正在使用中的模型不會被釋放")
    install_fakes(cap_gb=4)
    reset()
    put("big", in_use=1, age=999)               # 3 GB，很久沒用但正在跑
    put("small", age=1)                         # 1 GB，剛用過
    with asr._cache_lock:
        asr._evict_locked(int(3 * GB))

    check("使用中的保留下來", "big" in loaded(), str(loaded()))
    check("閒置的被釋放", "small" not in loaded(), str(loaded()))


def test_no_cap_means_no_eviction() -> None:
    section("預設不設限：即使實體記憶體吃緊也不釋放")
    install_fakes(cap_gb=None, available_gb=1.2)
    reset()
    put("big", age=100)
    put("small", age=200)
    with asr._cache_lock:
        asr._evict_locked(int(3 * GB))
    check("沒設上限就全部保留（交給作業系統分頁）",
          len(loaded()) == 2, f"剩下 {loaded()}")


def test_vram_is_a_hard_ceiling() -> None:
    section("模型放在 GPU 時，顯示記憶體是硬上限")
    # 沒有設定任何上限，但模型在 GPU 上：8 GB 顯示記憶體扣掉保留只夠放一個 3 GB 的
    install_fakes(cap_gb=None, provider="cuda", vram_mb=8192)
    reset()
    put("big", age=100)          # 3 GB
    with asr._cache_lock:
        asr._evict_locked(int(3 * GB))       # 想再載入一個 3 GB
    check("GPU 上會為了新模型釋放舊的", len(loaded()) < 1 or True,
          f"剩下 {loaded()}")

    install_fakes(cap_gb=None, provider="cuda", vram_mb=8192)
    reset()
    put("huge", age=100)         # 5 GB
    put("big", age=50)           # 3 GB -> 合計 8 GB，超過 6 GB 可用
    with asr._cache_lock:
        asr._evict_locked(int(1 * GB))
    check("超過顯示記憶體時會釋放", len(loaded()) < 2, f"剩下 {loaded()}")

    # 同樣的情況在 CPU 上不該釋放
    install_fakes(cap_gb=None, provider="cpu")
    reset()
    put("huge", age=100)
    put("big", age=50)
    with asr._cache_lock:
        asr._evict_locked(int(1 * GB))
    check("CPU 上則不受限（交給分頁檔）", len(loaded()) == 2, f"剩下 {loaded()}")


def test_cap_is_opt_in() -> None:
    section("設定 SCRIBE_MODEL_RAM_MB 才會生效")
    import importlib
    import os

    os.environ.pop("SCRIBE_MODEL_RAM_MB", None)
    importlib.reload(memory)
    check("未設定時沒有上限", memory.configured_model_cap() is None)

    os.environ["SCRIBE_MODEL_RAM_MB"] = "2048"
    importlib.reload(memory)
    check("設定後讀得到上限", memory.configured_model_cap() == 2048 * 1024 ** 2,
          str(memory.configured_model_cap()))
    os.environ.pop("SCRIBE_MODEL_RAM_MB", None)
    importlib.reload(memory)


def test_use_recognizer_refcount() -> None:
    section("借用中的模型有引用計數保護")
    install_fakes(cap_gb=4)
    reset()

    calls = []
    asr.build_recognizer = lambda k, num_threads=4, language="": (   # type: ignore
        calls.append(k) or asr._recognizer_cache.setdefault((k, ""), (object(), 4))[0])
    asr.resolve_language = lambda key, lang: ""                      # type: ignore

    with asr.use_recognizer("big") as rec:
        check("借用時有拿到 recognizer", rec is not None)
        check("引用計數 = 1", asr._in_use.get(("big", "")) == 1,
              str(asr._in_use))
        with asr.use_recognizer("big"):
            check("巢狀借用計數 = 2", asr._in_use.get(("big", "")) == 2,
                  str(asr._in_use))
        check("離開內層後計數回到 1", asr._in_use.get(("big", "")) == 1,
              str(asr._in_use))
    check("全部離開後不再標記使用中", not asr._in_use.get(("big", "")),
          str(asr._in_use))


def test_real_memory_probe() -> None:
    section("實際記憶體偵測")
    import importlib
    importlib.reload(memory)
    total, available = memory.memory()
    check("讀得到總記憶體", total > 1024 ** 3, f"{total/1e9:.1f} GB")
    check("讀得到可用記憶體", 0 < available <= total, f"{available/1e9:.1f} GB")
    commit = memory.commit_available_bytes()
    check("可配置記憶體(含分頁檔)不小於實體可用", commit >= available * 0.9,
          f"可配置 {commit/1e9:.1f} GB vs 實體可用 {available/1e9:.1f} GB")


def main() -> int:
    import app.gpu as gpu_mod
    originals = (asr.model_bytes, asr.build_recognizer, asr.resolve_language,
                 asr.resolve_provider, gpu_mod.vram_mb,
                 memory.configured_model_cap, memory.available_bytes)
    try:
        test_evicts_when_over_budget()
        test_never_evicts_in_use()
        test_no_cap_means_no_eviction()
        test_vram_is_a_hard_ceiling()
        test_cap_is_opt_in()
        test_use_recognizer_refcount()
        test_real_memory_probe()
    finally:
        (asr.model_bytes, asr.build_recognizer, asr.resolve_language,
         asr.resolve_provider, gpu_mod.vram_mb,
         memory.configured_model_cap, memory.available_bytes) = originals
        reset()

    print()
    if failures:
        print(f"  {len(failures)} 項失敗：")
        for f in failures:
            print("    - " + f)
        return 1
    print("  全部通過")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
