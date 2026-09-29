"""Bringing an existing transcript up to date with the current text fixes.

The vocabulary and punctuation passes run at the end of recognition, so a
transcript is correct for the rules that existed *at the time*. Add a rule
afterwards and every older meeting keeps the error - measured on a real
25-minute meeting, a rule matching `定時結果` was in the list while the saved
transcript still said `定時結果`, because nobody had pressed the button.

So the passes have to be re-runnable over a finished project, and the project
has to be able to say whether re-running would change anything - otherwise
there is no way to know the button is worth pressing.

Run:  .venv/Scripts/python.exe tests/test_tidy.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import punctuation, tidy                              # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


RULES = [{"wrong": "定時結果", "right": "定序結果", "whole_word": False}]


def make_project() -> dict:
    return {
        "id": "p-test",
        "speakers": [{"id": "S1", "name": "甲"}, {"id": "S2", "name": "乙"}],
        "segments": [
            {"id": "seg-0000", "start": 0.0, "end": 4.0, "speaker": "S1",
             "text": "這裡的定時結果很重要。後面這句是另一句。",
             "original_text": "這裡的定時結果很重要。後面這句是另一句。",
             "original_speaker": "S1", "edited": False, "speaker_edited": False},
            {"id": "seg-0001", "start": 4.0, "end": 6.0, "speaker": "S2",
             "text": "我自己改過的定時結果",
             "original_text": "原本的樣子", "original_speaker": "S2",
             "edited": True, "speaker_edited": False},
        ],
    }


def test_vocabulary_rerun() -> None:
    section("詞語修正可以套用到已完成的逐字稿")
    proj = make_project()
    counts = tidy.apply_all(proj, rules=RULES)

    check("舊的錯字被修好", "定序結果" in proj["segments"][0]["text"],
          proj["segments"][0]["text"][:30])
    check("回報改了幾處", counts["vocabulary"] >= 1, str(counts))

    edited = [s for s in proj["segments"] if s.get("edited")][0]
    check("使用者改過的句子不會被動到", edited["text"] == "我自己改過的定時結果",
          edited["text"])


def test_pending_does_not_mutate() -> None:
    section("查詢待辦不會動到資料")
    proj = make_project()
    before = copy.deepcopy(proj)
    counts = tidy.pending(proj, rules=RULES)

    check("查詢後專案完全沒變", proj == before)
    check("查得到有東西要修", counts["vocabulary"] >= 1, str(counts))
    check("有 total 欄位", "total" in counts and counts["total"] >= 1, str(counts))


def test_pending_clean_project() -> None:
    section("已經整理過的逐字稿不會再提示")
    proj = make_project()
    tidy.apply_all(proj, rules=RULES)
    counts = tidy.pending(proj, rules=RULES)
    check("再查一次沒有待辦了", counts["total"] == 0, str(counts))


def test_split() -> None:
    section("整理時會順便依標點切句")
    proj = make_project()
    tidy.apply_all(proj, rules=RULES)

    ids = [s["id"] for s in proj["segments"]]
    check("兩句的段落被切開", len(proj["segments"]) >= 3, f"{len(ids)} 段")
    check("id 沒有重複", len(ids) == len(set(ids)), str(ids))
    check("依開始時間排序",
          all(proj["segments"][i]["start"] <= proj["segments"][i + 1]["start"]
              for i in range(len(proj["segments"]) - 1)))
    check("切出來的句子有講者",
          all(s["speaker"] for s in proj["segments"]))
    check("切出來的句子不會被標成使用者改過",
          all(s["text"] == s["original_text"]
              for s in proj["segments"] if not s.get("edited")))


def test_idempotent() -> None:
    section("整理兩次不會越整理越碎")
    proj = make_project()
    tidy.apply_all(proj, rules=RULES)
    after_one = copy.deepcopy(proj["segments"])
    counts = tidy.apply_all(proj, rules=RULES)
    check("第二次沒有再改任何東西", counts["total"] == 0, str(counts))
    check("段落數沒有再增加", len(proj["segments"]) == len(after_one),
          f"{len(after_one)} -> {len(proj['segments'])}")


def test_punctuation_pass() -> None:
    section("標點還原也會套用到舊的逐字稿")
    if not punctuation.available():
        check("標點還原", True, "（模型未下載，略過）")
        return
    proj = {
        "id": "p-test2",
        "speakers": [{"id": "S1", "name": "甲"}],
        "segments": [{
            "id": "seg-0000", "start": 0.0, "end": 12.0, "speaker": "S1",
            "text": "這東西其實裡面定序的結果全部都不是我們做的全部也都不是來自於外媒體公司那邊",
            "original_text": "這東西其實裡面定序的結果全部都不是我們做的全部也都不是來自於外媒體公司那邊",
            "original_speaker": "S1", "edited": False, "speaker_edited": False,
        }],
    }
    counts = tidy.apply_all(proj, rules=[])
    joined = "".join(s["text"] for s in proj["segments"])
    check("沒有標點的句子被補上標點", "，" in joined or "。" in joined, joined[:40])
    check("回報補了幾句", counts["punctuation"] >= 1, str(counts))
    check("字沒有被改掉",
          "".join(c for c in joined if c not in "，。？！、；：")
          == "這東西其實裡面定序的結果全部都不是我們做的全部也都不是來自於外媒體公司那邊",
          joined[:50])


def test_pending_agrees_with_apply() -> None:
    """pending() must promise exactly what apply_all() delivers.

    Found on the real meeting: pending kept reporting 2 outstanding splits
    that pressing the button never cleared. pending predicted with
    split_text(), apply_all() acts with split_span(), and split_span merges a
    piece too short to deserve its own row. A trailing "乙。" on a two-second
    segment is such a piece, so it was promised and never delivered.
    """
    section("pending 說的和實際做的必須一致")
    proj = {
        "id": "p-test4",
        "speakers": [{"id": "S1", "name": "甲"}],
        "segments": [{
            "id": "seg-0000", "start": 0.0, "end": 2.0, "speaker": "S1",
            "text": "甲" * 50 + "。乙。",
            "original_text": "甲" * 50 + "。乙。", "original_speaker": "S1",
            "edited": False, "speaker_edited": False,
        }],
    }
    promised = tidy.pending(proj, rules=[])["split"]
    before = len(proj["segments"])
    delivered = tidy.apply_all(proj, rules=[])["split"]
    check("承諾要切的數量等於真的切的數量", promised == delivered,
          f"pending 說 {promised}，實際 {delivered}")
    check("再查一次就沒有待辦了",
          tidy.pending(proj, rules=[])["split"] == 0,
          str(tidy.pending(proj, rules=[])))
    check("（參考）段落數", True, f"{before} -> {len(proj['segments'])}")

    # Same trap on the punctuation side: pending ignores a line too short for
    # the model to help with, so apply_all has to ignore it too, or the
    # endpoint keeps reporting one changed sentence that pending never saw.
    if punctuation.available():
        short = {
            "id": "p-test5",
            "speakers": [{"id": "S1", "name": "甲"}],
            "segments": [{
                "id": "seg-0000", "start": 0.0, "end": 1.0, "speaker": "S1",
                "text": "嗯嗯對啊好",          # under the worth-punctuating floor
                "original_text": "嗯嗯對啊好", "original_speaker": "S1",
                "edited": False, "speaker_edited": False,
            }],
        }
        promised_p = tidy.pending(short, rules=[])["punctuation"]
        delivered_p = tidy.apply_all(short, rules=[])["punctuation"]
        check("太短的句子兩邊都不處理", promised_p == delivered_p == 0,
              f"pending {promised_p}，實際 {delivered_p}")


def test_converges_on_unpunctuated_text() -> None:
    """The badge has to be able to reach zero.

    Found by running the real 25-minute meeting through the endpoint twice:
    the second pass still reported `punctuation: 1`, and `pending` still
    claimed 2 splits. Two separate causes.

    * The punctuation gate is a density threshold. A long line that the model
      only put one mark into is still under the threshold, so it is offered
      again for ever - and the model may answer differently each time.
    * `pending` predicted splits with split_text(), but apply_all() uses
      split_span(), which merges pieces too short to be worth a row. A
      segment the first would cut and the second would not was reported as
      outstanding work that pressing the button never cleared.
    """
    section("整理過後就真的乾淨了")
    if not punctuation.available():
        check("收斂", True, "（模型未下載，略過）")
        return

    proj = {
        "id": "p-test3",
        "speakers": [{"id": "S1", "name": "甲"}],
        "segments": [
            {"id": f"seg-{i:04d}", "start": i * 20.0, "end": i * 20.0 + 20.0,
             "speaker": "S1",
             "text": "這東西其實裡面定序的結果全部都不是我們做的全部也都不是來自於外媒體公司那邊"
                     "那邊全部都是我們在這個好幾論文看到有什麼關係的人在一起對然後",
             "original_text": "x", "original_speaker": "S1",
             "edited": False, "speaker_edited": False}
            for i in range(4)
        ],
    }
    first = tidy.apply_all(proj, rules=[])
    check("第一次有事情做", first["total"] > 0, str(first))

    left = tidy.pending(proj, rules=[])
    check("整理完之後 pending 歸零", left["total"] == 0, str(left))

    second = tidy.apply_all(proj, rules=[])
    check("再整理一次什麼都沒變", second["total"] == 0, str(second))

    n = len(proj["segments"])
    tidy.apply_all(proj, rules=[])
    check("段落數不再變動", len(proj["segments"]) == n,
          f"{n} -> {len(proj['segments'])}")


def main() -> int:
    test_vocabulary_rerun()
    test_pending_does_not_mutate()
    test_pending_clean_project()
    test_split()
    test_idempotent()
    test_punctuation_pass()
    test_pending_agrees_with_apply()
    test_converges_on_unpunctuated_text()

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
