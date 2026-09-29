"""Put the commas and full stops back.

Half the catalogue returns text with no punctuation at all. A meeting
transcribed by Cohere Transcribe came back as 7,800 characters carrying 43
punctuation marks - readable only if you already know what was said. The
recogniser that reads a recording best and the one that punctuates best are not
the same model, and a transcript should not have to be a trade between them.

A CT-Transformer runs over the finished text and inserts the marks, so any
recogniser's output can be read like prose. It is optional: without the model
downloaded, transcripts come out exactly as before.
"""
from __future__ import annotations

import logging
import re
import threading

from . import config as C
from .config import ASR_MODELS, is_downloaded, model_paths

log = logging.getLogger("scribe.punctuation")

KEY = "punct-ct-transformer"

_guard = threading.Lock()
_model = None            # sherpa_onnx.OfflinePunctuation, built once
_failed = False          # don't retry a load that already blew up

# The model was trained on Simplified Chinese and emits half-width marks. A
# transcript converted to Traditional wants the full-width forms beside it.
_WIDEN = {",": "，", "?": "？", "!": "！", ";": "；", ":": "："}


def available() -> bool:
    """True when the model is on disk - the only thing the caller must check."""
    return is_downloaded(KEY)


def _load():
    """Build the punctuation model once and keep it. None if unavailable."""
    global _model, _failed
    if _model is not None or _failed:
        return _model
    with _guard:
        if _model is not None or _failed:
            return _model
        if not available():
            return None
        try:
            import sherpa_onnx
            path = str(model_paths(KEY)["model"])
            # CPU only. The work is a few hundred short strings; measured
            # against the recogniser it is noise, and it avoids competing with
            # the ASR pass for VRAM.
            cfg = sherpa_onnx.OfflinePunctuationConfig(
                model=sherpa_onnx.OfflinePunctuationModelConfig(
                    ct_transformer=path, num_threads=C.DEFAULT_THREADS,
                    provider="cpu"))
            _model = sherpa_onnx.OfflinePunctuation(cfg)
            log.info("標點還原模型已載入")
        except Exception as exc:
            # A missing add-on must never cost someone their transcript.
            _failed = True
            log.warning("標點還原模型載入失敗，略過加標點：%s", exc)
    return _model


def _density(text: str) -> float:
    """Punctuation marks per character."""
    if not text:
        return 0.0
    return len(re.findall(r"[，。？！、,.?!;:]", text)) / len(text)


def restore(text: str) -> str:
    """Add punctuation to one line. Returns it unchanged if that isn't possible."""
    model = _load()
    if model is None or not text.strip():
        return text
    try:
        out = model.add_punctuation(text)
    except Exception as exc:
        log.warning("加標點失敗：%s", exc)
        return text
    if not out or not out.strip():
        return text
    for plain, wide in _WIDEN.items():
        out = out.replace(plain, wide)
    return collapse_marks(out.strip())


# The model is trained on unpunctuated Simplified text, so a mark the
# recogniser already wrote is invisible to it and it inserts its own right
# beside it. Cohere emits a half-width "?", and a long sparsely punctuated
# line slips under the density gate, so transcripts came back reading
# "互相對於的？。" and "什麼 parameters。？首先".
#
# Collapsing afterwards is safer than stripping beforehand: stripping would
# have to remove "." and ",", which would turn "3.5" into "35" and change the
# words. This only ever deletes a mark that sits next to another one.
_MARK_RANK = {"！": 4, "?": 4, "？": 4, "!": 4, "。": 3, "；": 2, ";": 2,
              "，": 1, ",": 1, "、": 1, "：": 1, ":": 1}
_MARK_RUN = re.compile(r"[，。？！、；：,.?!;:]{2,}")


def collapse_marks(text: str) -> str:
    """Reduce any run of adjacent punctuation to its strongest single mark."""
    def pick(match: re.Match) -> str:
        run = match.group(0)
        return max(run, key=lambda ch: _MARK_RANK.get(ch, 0))

    return _MARK_RUN.sub(pick, text)


def needs_punctuation(text: str) -> bool:
    """Whether restore_if_needed() would do anything - without loading a model.

    Same gate, cheap half. Lets a caller count the work before doing it.
    """
    return bool(text) and _density(text) < 0.02


def restore_if_needed(text: str) -> str:
    """Punctuate a line unless it already carries punctuation.

    Re-punctuating text that has marks only moved them around, so anything
    already at a normal density is left exactly as the recogniser wrote it.
    """
    if not text or _density(text) >= 0.02:
        return text
    return restore(text)


def should_apply(model_key: str) -> bool:
    """Whether this recogniser's output needs help."""
    if ASR_MODELS.get(model_key, {}).get("punctuates"):
        return False
    return available()
