"""What an uploaded filename turns into.

secure_filename() throws away every non-ASCII character. For a tool whose
recordings are named in Chinese that is not a rounding error - it destroys the
extension too:

    secure_filename("孟青宴安討論.m4a")  ->  "m4a"

Three things broke off the back of that. The suffix came out empty, so the
"is this a supported format?" check was skipped entirely, because it only
fires when a suffix is present. The stored upload became `source` with no
extension. And the fallback project name became "m4a" - which the upload
dialog hits for real, since it deliberately sends an empty name when several
files are selected at once.

The sanitising was never protecting anything: the upload is written to
`source<suffix>` inside a uuid-named folder, so the user's filename never
reaches the filesystem. Only the suffix does, and that is whitelisted.

Run:  .venv/Scripts/python.exe tests/test_upload.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.audio import SUPPORTED_SUFFIXES, upload_name_and_suffix   # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def test_chinese_names() -> None:
    section("中文檔名不會被吃掉")
    name, suffix = upload_name_and_suffix("孟青宴安討論.m4a")
    check("副檔名留得住", suffix == ".m4a", repr(suffix))
    check("名字留得住", name == "孟青宴安討論", repr(name))

    name, suffix = upload_name_and_suffix("08-25 規範討論.mp3")
    check("中英混合也可以", (name, suffix) == ("08-25 規範討論", ".mp3"),
          f"{name!r} {suffix!r}")

    name, suffix = upload_name_and_suffix("録音.MP3")
    check("副檔名一律轉小寫", suffix == ".mp3", repr(suffix))


def test_plain_names() -> None:
    section("一般檔名維持原本行為")
    check("英文檔名", upload_name_and_suffix("meeting.m4a") == ("meeting", ".m4a"))
    check("空白轉底線不再需要，名字原樣保留",
          upload_name_and_suffix("a b.m4a") == ("a b", ".m4a"),
          str(upload_name_and_suffix("a b.m4a")))


def test_edge_cases() -> None:
    section("奇怪的輸入")
    check("沒有副檔名時副檔名為空",
          upload_name_and_suffix("recording")[1] == "",
          repr(upload_name_and_suffix("recording")[1]))
    check("完全沒有檔名時有預設名",
          upload_name_and_suffix("")[0] != "",
          repr(upload_name_and_suffix("")[0]))

    # The suffix is the only part that reaches the filesystem, so it must not
    # be able to carry a path out of the project folder.
    for nasty in ["../../../etc/passwd.wav", r"..\..\win.ini.wav",
                  "/tmp/x.wav", "C:/Windows/y.wav"]:
        name, suffix = upload_name_and_suffix(nasty)
        check(f"{nasty!r} 不會帶出路徑",
              "/" not in name and "\\" not in name
              and "/" not in suffix and "\\" not in suffix
              and ".." not in suffix,
              f"name={name!r} suffix={suffix!r}")

    check("看起來像副檔名的中文名不會被誤判",
          upload_name_and_suffix("會議.wav") == ("會議", ".wav"),
          str(upload_name_and_suffix("會議.wav")))


def test_format_gate_now_fires() -> None:
    section("格式檢查不會再被跳過")
    # Previously this produced an empty suffix, so `if suffix and ...` never
    # ran and an unsupported file sailed through to a confusing decode error.
    _, suffix = upload_name_and_suffix("報告.pdf")
    check("不支援的中文檔名會被抓到",
          suffix == ".pdf" and suffix not in SUPPORTED_SUFFIXES, repr(suffix))
    _, suffix = upload_name_and_suffix("會議.m4a")
    check("支援的中文檔名會通過",
          suffix in SUPPORTED_SUFFIXES, repr(suffix))


def main() -> int:
    test_chinese_names()
    test_plain_names()
    test_edge_cases()
    test_format_gate_now_fires()

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
