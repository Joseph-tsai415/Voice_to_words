"""Showing sentences while the recognition is still running.

A 25-minute meeting takes minutes to transcribe, and until now the screen said
"辨識完成後，逐字稿會出現在這裡" for the whole of it. Everything needed to show
progress is already there - the ASR loop decodes in batches of ASR_BATCH and
appends to a list - so the results can be written out as they arrive.

The trap is the speaker table. populate() derives it from whichever utterances
it was handed, numbering them S1, S2, ... in cluster order. Do that on a
partial list and the *next* batch can introduce a lower cluster id, renumbering
every speaker underneath the user. Diarization finishes before recognition
starts, so the full cluster set is known up front and must be passed in.

Run:  .venv/Scripts/python.exe tests/test_partial.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import project as store                            # noqa: E402
from app.asr import Utterance                               # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


# Cluster 7 speaks first; cluster 2 only appears in the second batch. That is
# the ordering that renumbers the speakers if the table is rebuilt per batch.
BATCH_1 = [Utterance(0.0, 2.0, 7, "第一句"), Utterance(2.0, 4.0, 7, "第二句")]
BATCH_2 = BATCH_1 + [Utterance(4.0, 6.0, 2, "第三句")]
BATCH_3 = BATCH_2 + [Utterance(6.0, 8.0, 7, "第四句")]
CLUSTERS = [2, 7]                     # what diarization found, known up front


def run() -> None:
    section("辨識中就先寫出已完成的句子")
    pid = None
    try:
        proj = store.create("部分結果測試", 0.0, "sense-voice", "s2twp", {})
        pid = proj["id"]

        after1 = store.populate_progress(pid, BATCH_1, CLUSTERS)
        check("第一批就寫進去了", len(after1["segments"]) == 2,
              f"{len(after1['segments'])} 句")
        check("狀態仍然是處理中，不是完成", after1["status"] != "ready",
              after1["status"])

        first_ids = {s["cluster"]: s["id"] for s in after1["speakers"]}
        check("講者表用的是完整的分群，不是這一批看到的",
              len(after1["speakers"]) == 2, f"{len(after1['speakers'])} 位")

        after2 = store.populate_progress(pid, BATCH_2, CLUSTERS)
        check("第二批累加上去", len(after2["segments"]) == 3,
              f"{len(after2['segments'])} 句")
        second_ids = {s["cluster"]: s["id"] for s in after2["speakers"]}
        check("講者編號沒有因為新的人出現而重排", first_ids == second_ids,
              f"{first_ids} -> {second_ids}")

        # The same text must not move to a different speaker between batches.
        check("已經寫出去的句子不會改講者",
              [s["speaker"] for s in after1["segments"]]
              == [s["speaker"] for s in after2["segments"][:2]])

        final = store.populate(pid, BATCH_3, CLUSTERS)
        check("最後收尾寫完整份", len(final["segments"]) == 4,
              f"{len(final['segments'])} 句")
        check("收尾後狀態變成完成", final["status"] == "ready", final["status"])
        final_ids = {s["cluster"]: s["id"] for s in final["speakers"]}
        check("收尾用的講者編號和過程中一致", final_ids == first_ids,
              f"{first_ids} -> {final_ids}")

        # Without an explicit cluster list populate() must behave as before.
        legacy = store.populate(pid, BATCH_3)
        check("沒給分群時維持原本行為", len(legacy["speakers"]) == 2,
              f"{len(legacy['speakers'])} 位")
    finally:
        if pid:
            shutil.rmtree(store.project_dir(pid), ignore_errors=True)


def main() -> int:
    run()
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
