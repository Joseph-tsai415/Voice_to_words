"""A voiceprint library, so S1 is Jimmy again next time.

Speaker ids are per-project and meaningless across runs: diarization hands
out cluster numbers in whatever order it finds them, so re-running a meeting
loses every name you typed. The embeddings that would fix this already exist
- voiceprint.py builds them to put re-run sentences back under the right
speaker - they were just never kept anywhere.

`data/speakers.json` keeps a named unit vector per person. Naming a speaker
remembers them; a later meeting matches each cluster against the library and
fills the name in.

Thresholds are voiceprint.py's, measured on this pipeline: same speaker
~0.88, different speakers ~0.33, match at 0.60.

Run:  .venv/Scripts/python.exe tests/test_speakers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                          # noqa: E402

from app import speakers                                    # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def unit(seed: int, like: np.ndarray | None = None, mix: float = 0.0) -> np.ndarray:
    """A unit vector; with `like`+`mix`, one at a chosen similarity to it.

    Both parts must be normalised before mixing. 512-dimensional random
    vectors are near-orthogonal, so an unnormalised one (norm ~22) swamps a
    unit vector and the result is nowhere near the similarity asked for -
    which is what made the first version of this test fail.
    Resulting cosine is mix / sqrt(mix^2 + (1-mix)^2); mix=0.65 gives ~0.88,
    the same-speaker figure measured on this pipeline.
    """
    rng = np.random.default_rng(seed)
    v = rng.normal(size=512).astype(np.float32)
    v /= float(np.linalg.norm(v))
    if like is not None:
        v = mix * like + (1 - mix) * v
        v /= float(np.linalg.norm(v))
    return v


def test_placeholder_names() -> None:
    section("只有真名字才值得記")
    check("講者 1 不是名字", speakers.is_placeholder("講者 1"))
    check("講者 12 不是名字", speakers.is_placeholder("講者 12"))
    check("Speaker 2 不是名字", speakers.is_placeholder("Speaker 2"))
    check("空白不是名字", speakers.is_placeholder("   "))
    check("Jimmy 是名字", not speakers.is_placeholder("Jimmy"))
    check("中文名字也算", not speakers.is_placeholder("林主任"))


def test_remember_and_identify(tmp) -> None:
    section("記住一個聲音，之後認得出來")
    jimmy = unit(1)
    other = unit(2)

    check("記住 Jimmy", speakers.remember("Jimmy", jimmy))
    check("佔位名字不會被記", not speakers.remember("講者 3", unit(3)))
    check("沒有向量就不記", not speakers.remember("Nobody", None))

    name, score = speakers.identify(jimmy)
    check("同一段聲音認得出來", name == "Jimmy" and score > 0.99,
          f"{name} {score:.3f}")

    name, score = speakers.identify(other)
    check("不同的人不會被誤認", name is None, f"{name} {score:.3f}")

    # Close but not identical, as a second recording of one person would be.
    # mix=0.65 lands at ~0.88, the measured same-speaker similarity.
    name, score = speakers.identify(unit(4, like=jimmy, mix=0.65))
    check("同一個人換一段錄音仍然認得（相似度 ~0.88）",
          name == "Jimmy" and score > 0.80, f"{name} {score:.3f}")

    # ~0.33 is the measured different-speaker figure; it must stay rejected.
    name, score = speakers.identify(unit(5, like=jimmy, mix=0.26))
    check("不同人偶然有點像也不會被認成 Jimmy（相似度 ~0.33）",
          name is None, f"{name} {score:.3f}")


def test_refine_not_replace(tmp) -> None:
    section("同一個人再記一次是加強，不是覆蓋")
    base = unit(10)
    speakers.remember("Rebecca", base)
    first = speakers.load_library()["Rebecca"]

    speakers.remember("Rebecca", unit(11, like=base, mix=0.9))
    lib = speakers.load_library()
    check("還是只有一個 Rebecca", list(lib).count("Rebecca") == 1)
    check("向量有變（有被更新）", not np.allclose(lib["Rebecca"], first))
    check("更新後仍是單位長度",
          abs(float(np.linalg.norm(lib["Rebecca"])) - 1.0) < 1e-5)
    check("更新後還是認得原本那段", speakers.identify(base)[0] == "Rebecca")
    check("有記錄累積了幾次", speakers.sample_count("Rebecca") == 2,
          str(speakers.sample_count("Rebecca")))


def test_persistence(tmp) -> None:
    section("存到檔案，下次打開還在")
    speakers.remember("Ada", unit(20))
    speakers.reload()                       # drop any in-memory state
    lib = speakers.load_library()
    check("重新讀取後還在", "Ada" in lib, str(list(lib)))
    check("存的是單位向量",
          abs(float(np.linalg.norm(lib["Ada"])) - 1.0) < 1e-5)
    check("檔案真的在 data/ 底下", speakers.LIBRARY_PATH.exists(),
          str(speakers.LIBRARY_PATH))


def test_forget(tmp) -> None:
    section("可以刪掉")
    speakers.remember("Temp", unit(30))
    check("刪掉回報成功", speakers.forget("Temp"))
    check("刪掉後認不出來", speakers.identify(unit(30))[0] != "Temp")
    check("刪一個不存在的回報 False", not speakers.forget("NoSuchPerson"))


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        # Never touch the real library while testing.
        speakers.LIBRARY_PATH = Path(d) / "speakers.json"
        speakers.reload()
        test_placeholder_names()
        test_remember_and_identify(d)
        test_refine_not_replace(d)
        test_persistence(d)
        test_forget(d)

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
