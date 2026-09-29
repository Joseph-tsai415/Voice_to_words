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


def main() -> int:
    test_length_cap()
    test_role_gate()
    test_special_tokens()
    test_punctuation()

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
