"""Post-processing applied to raw ASR output before it reaches the transcript.

SenseVoice, Paraformer and FireRedASR all emit Simplified Chinese regardless of
what was spoken, so a Traditional-Chinese meeting needs converting back.
"""
from __future__ import annotations

import functools
import re

# OpenCC conversion profiles worth exposing; the value is the OpenCC config name.
# Order matters: this drives the dropdown, and "不轉換" sitting first meant a
# Chinese meeting could come out entirely in simplified characters just by
# leaving the default alone. The useful choice goes first, "do nothing" last.
ZH_MODES = {
    "s2twp": "s2twp",   # 簡 -> 繁(台灣，含用詞轉換：软件->軟體)
    "s2tw": "s2tw",     # 簡 -> 繁(台灣，僅字形)
    "s2hk": "s2hk",     # 簡 -> 繁(香港)
    "t2s": "t2s",       # 繁 -> 簡
    "none": None,
}

ZH_MODE_LABELS = {
    "s2twp": "簡轉繁（台灣用詞）",
    "s2tw": "簡轉繁（台灣字形）",
    "s2hk": "簡轉繁（香港）",
    "t2s": "繁轉簡",
    "none": "不轉換（保留原樣）",
}

DEFAULT_ZH_MODE = "s2twp"


@functools.lru_cache(maxsize=8)
def _converter(config: str):
    import opencc
    return opencc.OpenCC(config)


def convert_zh(text: str, mode: str = DEFAULT_ZH_MODE) -> str:
    config = ZH_MODES.get(mode)
    if not config or not text:
        return text
    try:
        return _converter(config).convert(text)
    except Exception:
        return text          # never let a conversion failure lose the transcript


# SenseVoice can leak emotion/event tags such as <|zh|><|NEUTRAL|><|Speech|>.
_TAG = re.compile(r"<\|[^|>]*\|>")

# Special tokens that leak out of some graphs as literal text. FireRedASR2
# writes "< sil >" - with the spaces, because it is emitting one token at a
# time - and it landed in finished transcripts as if someone had said it.
# Anchored on a known token list so a stray "<" in real speech survives.
_SPECIAL = re.compile(
    r"<\s*/?\s*(sil|silence|unk|blank|noise|music|eos|bos|pad|s)\s*>",
    re.IGNORECASE)

# CJK ideographs, CJK punctuation, and fullwidth forms (、。，！？ etc).
# Token-based models emit a space between every token, which reads wrong in
# Chinese but is required between Latin words - so only strip CJK<->CJK gaps.
_CJK = r"　-〿㐀-䶿一-鿿豈-﫿＀-￯"
_CJK_SPACE = re.compile(f"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
# A space before fullwidth punctuation is always wrong, even after Latin text.
_SPACE_BEFORE_PUNCT = re.compile(f"[ \t]+(?=[{_CJK}])(?=[，。、！？；：）】」』》])")


# Removing a token can strand the punctuation that sat beside it - otherwise
# "嗯嗯嗯，< sil >。" finishes as "嗯嗯嗯，。".
_DANGLING = re.compile(r"[，、]+(?=[。！？])")
_LEADING_PUNCT = re.compile(r"^[\s，、。！？；：]+")


def clean(text: str, zh_mode: str = DEFAULT_ZH_MODE) -> str:
    text = _TAG.sub("", text or "")
    text = _SPECIAL.sub("", text).strip()
    text = _CJK_SPACE.sub("", text)
    text = _SPACE_BEFORE_PUNCT.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _DANGLING.sub("", text)
    text = _LEADING_PUNCT.sub("", text)
    return convert_zh(text, zh_mode).strip()


# Sentences that carry no content. Diarization happily turns each of these into
# its own speaker, which is how a 3-person meeting reached 102 of them.
_PUNCT_ONLY = re.compile(r"^[\s。，、．,.!?！？…~～\-–—:：;；\"'‘’“”()（）\[\]【】]*$")

# Backchannels: real speech, but they say nothing and their audio is too short
# to identify anyone from.
BACKCHANNEL = {
    "嗯", "嗯嗯", "呃", "啊", "喔", "哦", "唉", "誒", "欸", "對", "對對", "好", "好的",
    "是", "是的", "有", "沒有", "OK", "ok", "Ok", "Yeah", "yeah", "Yes", "yes",
    "Mm", "mm", "Hmm", "hmm", "嗯哼", "這個", "那個",
}


def is_meaningful(text: str) -> bool:
    """True if this sentence is worth keeping as its own line."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _PUNCT_ONLY.match(stripped):
        return False
    core = re.sub(r"[\s。，、．,.!?！？…~～]", "", stripped)
    # A single character on its own line is almost always a mis-decoded noise
    # burst ("的。", "球。") rather than something anyone said.
    return len(core) > 1


def is_backchannel(text: str) -> bool:
    """A short acknowledgement - keep it, but do not trust it to identify anyone."""
    core = re.sub(r"[\s。，、．,.!?！？…~～]", "", (text or "").strip())
    return bool(core) and core in BACKCHANNEL


def looks_unfinished(text: str) -> bool:
    """Sentence has no terminal punctuation, so the speaker probably continues."""
    stripped = (text or "").rstrip()
    return bool(stripped) and stripped[-1] not in "。！？!?…"
