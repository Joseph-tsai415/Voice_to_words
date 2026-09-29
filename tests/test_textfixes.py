"""Segment length, the add-on role gate, and punctuation restoration.

Three things that each cost a real transcript:

* An uncapped segment. The safety net in split_by_speaker() bypasses the VAD to
  recover speech pyannote found, so it could emit a turn of any length. A
  41-second unit crashed the recogniser outright.
* A catalogue entry that cannot transcribe. The punctuation model downloads
  through the same machinery as the recognisers, so every place that picks a
  recogniser has to filter it out - including for unknown keys.
* Punctuation. Half the catalogue returns none at all.

Only the punctuation checks need a model on disk; they skip without one.

Run:  .venv/Scripts/python.exe tests/test_textfixes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as C                                    # noqa: E402
from app import punctuation                                    # noqa: E402
from app.asr import split_by_speaker                           # noqa: E402
from app.config import ASR_MODELS, recognizer_keys             # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  ✓" if ok else "  ✗") + " " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label + (f" ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print(title)


def test_length_cap() -> None:
    section("沒有一句會超過長度上限")
    cap = C.VAD_MAX_SPEECH_SEC

    # The safety-net path: pyannote heard 45 s, the VAD only marked 2 s of it.
    units = split_by_speaker([(0.0, 2.0)], [(0.0, 45.0, 0)])
    longest = max(e - s for s, e, _ in units)
    check("超長的發言會被切開", longest <= cap + 0.01, f"最長 {longest:.1f}s")
    check("切開後仍然涵蓋整段", abs(max(e for _, e, _ in units) - 45.0) < 0.01,
          f"結束於 {max(e for _, e, _ in units):.1f}s")
    check("切開後講者不變", {spk for _, _, spk in units} == {0},
          str({spk for _, _, spk in units}))

    # Normal-length speech must pass through untouched.
    units = split_by_speaker([(0.0, 5.0)], [(0.0, 5.0, 1)])
    check("正常長度不會被切", len(units) == 1, f"{len(units)} 段")


def test_role_gate() -> None:
    section("附加元件不能拿來辨識")

    def usable(key: str) -> bool:
        return (key in ASR_MODELS
                and ASR_MODELS[key].get("role", "asr") == "asr")

    check("辨識模型可用", usable("sense-voice"))
    check("標點模型不可用", not usable("punct-ct-transformer"))
    check("未知的 key 不可用", not usable("no-such-model"))
    check("recognizer_keys() 不含附加元件",
          "punct-ct-transformer" not in recognizer_keys(),
          str(recognizer_keys()))
    check("recognizer_keys() 含每個真正的辨識模型",
          all(k in recognizer_keys() for k in
              ("sense-voice", "cohere-transcribe", "fire-red-asr2")))

    # The tempting one-liner that silently lets unknown keys through.
    naive = ASR_MODELS.get("no-such-model", {}).get("role", "asr") == "asr"
    check("（記錄）單行版的 .get 預設會放行未知 key，所以不能那樣寫", naive)


def test_punctuation() -> None:
    section("標點還原")
    if not punctuation.available():
        check("標點還原", True, "（模型未下載，略過）")
        return

    plain = "這東西其實裡面定序的結果全部都不是我們做的全部也都不是來自於外媒體公司那邊"
    fixed = punctuation.restore_if_needed(plain)
    check("沒有標點的句子會補上標點", fixed != plain and "，" in fixed, fixed[:40])
    check("補標點不會改動字", "".join(c for c in fixed if c not in "，。？！、；：")
          == plain, fixed[:40])

    already = "呃，這邊的話你有看過嗎？他應該沒有看過。"
    check("已經有標點的句子原封不動",
          punctuation.restore_if_needed(already) == already)

    # The CT-Transformer is trained on unpunctuated Simplified text, so a mark
    # the recogniser already emitted is invisible to it and it inserts its own
    # right beside it. Cohere writes a half-width "?", and a long sparsely
    # punctuated line slips under the density gate, so real transcripts came
    # back reading "互相對於的？。" and "什麼 parameters。？首先".
    check("相鄰的重複標點只留一個",
          punctuation.collapse_marks("互相對於的？。那從") == "互相對於的？那從",
          punctuation.collapse_marks("互相對於的？。那從"))
    check("順序相反也一樣",
          punctuation.collapse_marks("parameters。？首先") == "parameters？首先",
          punctuation.collapse_marks("parameters。？首先"))
    check("逗號碰到句號留下句號",
          punctuation.collapse_marks("甲，。乙") == "甲。乙",
          punctuation.collapse_marks("甲，。乙"))
    check("正常的標點完全不動",
          punctuation.collapse_marks("你好，世界。") == "你好，世界。")
    check("小數點不會被動到",
          punctuation.collapse_marks("大約 3.5 毫米") == "大約 3.5 毫米")
    check("沒有標點時安全", punctuation.collapse_marks("甲乙丙") == "甲乙丙")

    messy = "但有沒有可能透過查說就是比如說這幾段RNA他們之間本身是互相對於的?那從理論上他可以先推出來說這幾段RNA可能有問題但是後面他還有一個 validation的 step就是主要的"
    out = punctuation.restore_if_needed(messy)
    import re as _re
    check("真實的句子不會產生連續兩個標點",
          not _re.search(r"[，。？！、；：]{2,}", out), out[:60])

    check("空字串安全", punctuation.restore_if_needed("") == "")
    check("自己會加標點的模型不套用",
          not punctuation.should_apply("sense-voice"))
    check("不加標點的模型會套用", punctuation.should_apply("cohere-transcribe"))


def test_special_tokens() -> None:
    section("模型漏出來的特殊標記會被清掉")
    from app.textproc import clean, is_meaningful

    # FireRedASR2 emits "< sil >" with spaces, one token at a time.
    check("< sil > 會被清掉", clean("< sil > < sil >。", "none") == "",
          repr(clean("< sil > < sil >。", "none")))
    check("整句只有標記時不會留下空句",
          not is_meaningful(clean("< sil > < sil >。", "none")))
    check("清掉後不會留下孤零零的逗號",
          clean("嗯嗯嗯，< sil >。", "none") == "嗯嗯嗯。",
          repr(clean("嗯嗯嗯，< sil >。", "none")))
    check("句首多餘標點會清掉", clean("，這東西其實", "none") == "這東西其實",
          repr(clean("，這東西其實", "none")))
    check("正常句子完全不動", clean("你好，世界。", "none") == "你好，世界。")
    check("真的講到的 < 不會被吃掉", clean("五 < 六", "none") == "五 < 六",
          repr(clean("五 < 六", "none")))


def test_sentence_split() -> None:
    """A segment is a VAD chunk, which can be a whole paragraph.

    Measured on a real 25-minute meeting: 41 of 118 segments ran past 80
    characters, the longest 357 characters over 60 seconds. You cannot hand a
    sentence of that back to a different speaker, because it is not its own
    row. Once punctuation is in, the terminal marks say where the sentences
    are, so the segment can be cut into them.
    """
    section("長句會依標點切成句子")
    from app.sentences import split_span, split_text

    check("沒有句尾標點就不切", split_text("甲乙丙") == ["甲乙丙"],
          str(split_text("甲乙丙")))
    check("句尾標點只在最後時不切", split_text("甲乙丙。") == ["甲乙丙。"],
          str(split_text("甲乙丙。")))
    check("兩句會切開", split_text("甲甲甲。乙乙乙。") == ["甲甲甲。", "乙乙乙。"],
          str(split_text("甲甲甲。乙乙乙。")))
    check("問號驚嘆號也算句尾",
          split_text("是嗎？真的！") == ["是嗎？", "真的！"],
          str(split_text("是嗎？真的！")))
    check("逗號不是句尾，不會切",
          split_text("甲，乙，丙") == ["甲，乙，丙"],
          str(split_text("甲，乙，丙")))
    # A half-width period is a decimal point far more often than a full stop
    # in a technical meeting, so it is deliberately not a boundary.
    check("小數點不會被當成句尾",
          split_text("大約 3.5 毫米") == ["大約 3.5 毫米"],
          str(split_text("大約 3.5 毫米")))
    check("空字串安全", split_text("") == [])

    # --- timing ---------------------------------------------------------
    pieces = split_span(0.0, 10.0, "甲甲甲甲甲。乙乙乙乙乙。")
    check("等長的兩句各分到一半", len(pieces) == 2
          and abs(pieces[0][1] - 5.0) < 0.01, str(pieces))

    pieces = split_span(0.0, 6.0, "甲。乙乙乙。")
    check("時間依字數分配", len(pieces) == 2 and abs(pieces[0][1] - 2.0) < 0.01,
          str([(round(s, 2), round(e, 2)) for s, e, _ in pieces]))

    pieces = split_span(3.0, 9.0, "甲甲甲。乙乙乙。丙丙丙。")
    check("起點與終點不變",
          abs(pieces[0][0] - 3.0) < 1e-6 and abs(pieces[-1][1] - 9.0) < 1e-6,
          str([(round(s, 2), round(e, 2)) for s, e, _ in pieces]))
    check("切開後時間完全接續，沒有空隙也沒有重疊",
          all(abs(pieces[i][1] - pieces[i + 1][0]) < 1e-6
              for i in range(len(pieces) - 1)), str(pieces))
    check("文字完全保留",
          "".join(t for _, _, t in pieces) == "甲甲甲。乙乙乙。丙丙丙。")

    # A one-character sentence in a long segment would get a few milliseconds.
    pieces = split_span(0.0, 1.0, "甲。" + "乙" * 99 + "。", min_sec=0.5)
    check("太短的片段會併回去，不會產生零點幾秒的句子",
          len(pieces) == 1, str([(round(s, 2), round(e, 2)) for s, e, _ in pieces]))

    check("沒有可切的地方時原樣回傳一段",
          split_span(2.0, 4.0, "甲乙丙") == [(2.0, 4.0, "甲乙丙")],
          str(split_span(2.0, 4.0, "甲乙丙")))


def test_long_run_fallback() -> None:
    """Terminal marks alone are not enough.

    Run over the real meeting, splitting on 。！？ took the longest segment
    from 357 characters to 230 and left 24 segments still past 80 - because
    the punctuation model mostly writes commas. A clause boundary is a worse
    cut than a sentence boundary, but far better than a 230-character row.
    """
    section("只有逗號的長句也會被切開")
    from app.sentences import split_text

    long_run = "，".join(["這邊大概是這樣子的一個情況"] * 8)      # ~110 chars
    pieces = split_text(long_run, max_chars=40)
    check("超長的逗號句會切開", len(pieces) > 1, f"{len(pieces)} 段")
    check("切完每段都在上限附近", max(len(p) for p in pieces) <= 55,
          f"最長 {max(len(p) for p in pieces)} 字")
    check("文字完全保留", "".join(pieces) == long_run)

    short = "甲，乙，丙"
    check("沒超過上限的逗號句不會被切",
          split_text(short, max_chars=40) == [short], str(split_text(short, max_chars=40)))

    # Cutting mid-word would be worse than a long row, so a run with no
    # punctuation at all is left exactly as it is.
    nothing = "甲" * 200
    check("完全沒有標點的長句不會被硬切",
          split_text(nothing, max_chars=40) == [nothing],
          f"{len(split_text(nothing, max_chars=40))} 段")

    check("句尾標點仍然優先",
          split_text("甲甲。乙乙。", max_chars=40) == ["甲甲。", "乙乙。"],
          str(split_text("甲甲。乙乙。", max_chars=40)))
    check("不給上限時維持原本行為",
          split_text(long_run) == [long_run], f"{len(split_text(long_run))} 段")


def test_split_utterances() -> None:
    section("辨識完成後整份逐字稿會切成句子")
    from app.asr import Utterance, split_utterances

    out = split_utterances([Utterance(0.0, 10.0, 1, "甲甲甲甲甲。乙乙乙乙乙。")])
    check("一段兩句會變成兩段", len(out) == 2, f"{len(out)} 段")
    check("講者不變", all(u.speaker == 1 for u in out))
    check("時間接續", abs(out[0].end - out[1].start) < 1e-6)
    check("涵蓋原本的範圍",
          abs(out[0].start - 0.0) < 1e-6 and abs(out[-1].end - 10.0) < 1e-6)

    unsplittable = [Utterance(0.0, 3.0, 2, "沒有句尾標點的一段話")]
    check("不能切的原樣保留", split_utterances(unsplittable) == unsplittable)

    check("空清單安全", split_utterances([]) == [])

    many = split_utterances([
        Utterance(0.0, 4.0, 1, "甲甲。乙乙。"),
        Utterance(4.0, 8.0, 2, "丙丙。丁丁。"),
    ])
    check("多段各自切開，順序不變", len(many) == 4 and
          [u.speaker for u in many] == [1, 1, 2, 2],
          str([(round(u.start, 1), round(u.end, 1), u.speaker) for u in many]))


def main() -> int:
    test_length_cap()
    test_role_gate()
    test_special_tokens()
    test_punctuation()
    test_sentence_split()
    test_long_run_fallback()
    test_split_utterances()

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
