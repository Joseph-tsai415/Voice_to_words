"""A re-run must not delete the recording it just used.

The worker ends a successful job with `source.unlink()`, which is meant to
throw away the original upload - `source.<ext>` is disposable once
`audio.wav` exists. But `_rerun_source()` deliberately hands a re-run
**audio.wav itself**, precisely because the upload is already gone by then.
So `source` and the project's only copy of the recording were the same file,
and every successful 重新辨識 deleted it.

Seen twice on a live install: a project left holding only `project.json`,
with no audio and therefore no way to ever run it again.

Run:  .venv/Scripts/python.exe tests/test_audio_kept.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import project as store                            # noqa: E402
from app.transcribe_queue import disposable_upload          # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def test_predicate() -> None:
    section("哪些檔案是用完可以丟的")
    folder = Path("/tmp/x/p-1")
    wav = folder / "audio.wav"

    check("原始上傳檔可以丟", disposable_upload(folder / "source.m4a", wav))
    check("沒有副檔名的舊上傳檔也可以丟", disposable_upload(folder / "source", wav))
    check("audio.wav 絕對不能丟", not disposable_upload(wav, wav))
    check("大小寫不同的 audio.wav 也不能丟",
          not disposable_upload(folder / "AUDIO.WAV", folder / "audio.wav"))
    check("不認得的檔案保守起見不丟",
          not disposable_upload(folder / "something-else.wav", wav))


def test_rerun_keeps_the_audio() -> None:
    """The real shape: a re-run is handed audio.wav as its source."""
    section("重新辨識之後音檔還在")
    proj = store.create("音檔保留測試", 0.0, "sense-voice", "s2twp", {})
    pid = proj["id"]
    folder = store.project_dir(pid)
    wav = folder / "audio.wav"
    try:
        wav.write_bytes(b"RIFF" + b"\0" * 2048)
        upload = folder / "source.m4a"
        upload.write_bytes(b"\0" * 512)

        # First run: source is the upload, so the upload goes and the wav stays.
        if disposable_upload(upload, wav):
            upload.unlink(missing_ok=True)
        check("第一次辨識後原始上傳檔被清掉", not upload.exists())
        check("第一次辨識後 audio.wav 還在", wav.exists())

        # Re-run: _rerun_source hands back audio.wav itself.
        rerun_source = wav
        if disposable_upload(rerun_source, wav):
            rerun_source.unlink(missing_ok=True)
        check("重新辨識後 audio.wav 仍然在（這就是原本的 bug）", wav.exists(),
              "audio.wav 被刪掉了" if not wav.exists() else "")
        check("音檔內容沒有被清空", wav.exists() and wav.stat().st_size > 0)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    test_predicate()
    test_rerun_keeps_the_audio()

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
