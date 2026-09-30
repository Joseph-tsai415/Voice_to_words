"""The installer and the catalogue have to agree.

Three ways these drift apart, all of which ship a broken first run:

* `DEFAULT_MODEL` names a model the setup scripts do not download, so the
  upload dialog opens on a recogniser that is not there.
* `setup.ps1` and `setup.sh` fall out of step, so Windows and everyone else
  get different models.
* A file list or byte count in a setup script stops matching the catalogue's
  `files` map, so the download lands somewhere `model_paths()` never looks.

None of these fail until someone installs from scratch, which is exactly
when nobody is watching. Parsing the scripts is crude, but it is the only
thing that ties a shell script to the Python catalogue.

Run:  .venv/Scripts/python.exe tests/test_setup_matches_config.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import (ASR_MODELS, DEFAULT_MODEL, model_dir,      # noqa: E402
                        recognizer_keys, required_files)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def ps_paths() -> set[str]:
    text = (ROOT / "setup.ps1").read_text(encoding="utf-8-sig")
    return {m.replace("\\", "/") for m in re.findall(r"Path\s*=\s*'([^']+)'", text)}


def sh_paths() -> set[str]:
    text = (ROOT / "setup.sh").read_text(encoding="utf-8")
    return set(re.findall(r'"(models/[^|"]+)\|', text))


def ps_sizes() -> list[str]:
    text = (ROOT / "setup.ps1").read_text(encoding="utf-8-sig")
    return sorted(re.findall(r"Size\s*=\s*(\d+)", text))


def sh_sizes() -> list[str]:
    text = (ROOT / "setup.sh").read_text(encoding="utf-8")
    return sorted(re.findall(r"\|(\d+)\"", text))


def test_default_model_is_sane() -> None:
    section("預設模型本身要說得通")
    check("預設模型在目錄裡", DEFAULT_MODEL in ASR_MODELS, DEFAULT_MODEL)
    check("預設模型真的能辨識（不是附加元件）",
          DEFAULT_MODEL in recognizer_keys(), DEFAULT_MODEL)


def test_setup_installs_the_default() -> None:
    section("安裝腳本要把預設模型裝起來")
    installed = ps_paths()
    folder = model_dir(DEFAULT_MODEL).relative_to(ROOT).as_posix()
    for name in required_files(DEFAULT_MODEL):
        want = f"{folder}/{name}"
        check(f"setup.ps1 會下載 {name}", want in installed, want)


def test_both_scripts_agree() -> None:
    section("兩個安裝腳本必須一致")
    p, s = ps_paths(), sh_paths()
    check("檔案清單相同", p == s,
          f"只在 ps1: {sorted(p - s)}　只在 sh: {sorted(s - p)}")
    check("位元組數相同", ps_sizes() == sh_sizes(),
          f"{ps_sizes()} vs {sh_sizes()}")
    check("每個檔案都有對應的大小", len(ps_sizes()) == len(p),
          f"{len(ps_sizes())} 個大小 vs {len(p)} 個檔案")


def test_shared_models_still_there() -> None:
    section("共用模型（VAD／分段／聲紋）不能被漏掉")
    for name in ("models/silero_vad.onnx",
                 "models/segmentation/model.onnx",
                 "models/speaker-embedding.onnx"):
        check(f"{name}", name in ps_paths())


def main() -> int:
    test_default_model_is_sane()
    test_setup_installs_the_default()
    test_both_scripts_agree()
    test_shared_models_still_there()

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
