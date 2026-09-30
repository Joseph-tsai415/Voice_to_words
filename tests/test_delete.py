"""Deleting a project has to finish, or say that it did not.

`delete()` was `shutil.rmtree(dir, ignore_errors=True)`. On Windows the
browser keeps `audio.wav` open for playback, so the tree walk removed
`project.json`, hit the locked wav, swallowed the error and returned as if it
had worked. What was left was a folder holding a 48 MB wav and no metadata -
and `list_projects()` skips any folder without a `project.json`, so the
orphan was invisible in the UI and could never be removed from it.

Found on a real install: one project folder with only `audio.wav`, one with
only `project.json`.

Run:  .venv/Scripts/python.exe tests/test_delete.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import project as store                            # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def test_normal_delete() -> None:
    section("一般刪除")
    proj = store.create("刪除測試", 0.0, "sense-voice", "s2twp", {})
    folder = store.project_dir(proj["id"])
    (folder / "audio.wav").write_bytes(b"x" * 1024)
    store.delete(proj["id"])
    check("整個資料夾都不見了", not folder.exists(), str(folder))


def test_locked_file_is_reported() -> None:
    """The case that actually happened: one file cannot be removed."""
    section("有檔案被鎖住時不能假裝成功")
    proj = store.create("鎖住測試", 0.0, "sense-voice", "s2twp", {})
    folder = store.project_dir(proj["id"])
    wav = folder / "audio.wav"
    wav.write_bytes(b"x" * 1024)

    handle = open(wav, "rb")          # Windows: now undeletable
    try:
        raised = False
        try:
            store.delete(proj["id"])
        except OSError:
            raised = True
        leftover = folder.exists()
        # Either it deleted everything (POSIX lets you unlink an open file),
        # or it failed loudly. Silently leaving an orphan is the bug.
        check("要嘛刪乾淨，要嘛報錯，不能默默留下孤兒",
              (not leftover) or raised,
              f"資料夾還在={leftover} 有報錯={raised}")
    finally:
        handle.close()
        shutil.rmtree(folder, ignore_errors=True)


def test_orphans_are_found() -> None:
    section("找得到殘留的孤兒資料夾")
    orphan = store.project_dir("p-orphan-test")
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "audio.wav").write_bytes(b"x" * 2048)
    try:
        found = store.find_orphans()
        ids = [o["id"] for o in found]
        check("沒有 project.json 的資料夾會被列出來", "p-orphan-test" in ids, str(ids))
        row = next((o for o in found if o["id"] == "p-orphan-test"), None)
        check("有回報佔用的空間", bool(row) and row["bytes"] >= 2048,
              str(row))
        # A healthy project must not be reported as an orphan.
        good = store.create("正常的", 0.0, "sense-voice", "s2twp", {})
        try:
            ids2 = [o["id"] for o in store.find_orphans()]
            check("正常的專案不會被當成孤兒", good["id"] not in ids2, str(ids2))
        finally:
            store.delete(good["id"])
    finally:
        shutil.rmtree(orphan, ignore_errors=True)


def main() -> int:
    test_normal_delete()
    test_locked_file_is_reported()
    test_orphans_are_found()

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
