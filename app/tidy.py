"""Bring a finished transcript up to date with the current text fixes.

The vocabulary and punctuation passes run at the end of recognition, which
makes a transcript correct for the rules that existed at that moment. Add a
rule next week and every older meeting silently keeps the error: on a real
25-minute meeting a rule matching `定時結果` sat in the list while the saved
transcript still read `定時結果`, because the fix is a button and nobody had
pressed it.

So the passes have to be re-runnable over a saved project, and the project
has to be able to report whether re-running would change anything - without
that, there is no way to know the button is worth pressing.

Order matters: vocabulary first (it works on words), then punctuation (it
needs the final words), then sentence splitting (it needs the marks).

Every pass skips a segment flagged `edited`. The user's own wording wins,
which is the same rule the live pipeline follows.
"""
from __future__ import annotations

import logging

from . import config as C
from . import punctuation, sentences, vocabulary

log = logging.getLogger("scribe.tidy")


def _targets(project: dict) -> list[dict]:
    return [s for s in project.get("segments", []) if not s.get("edited")]


# Below this many characters a line is "嗯嗯" or "對啊", and the punctuation
# model has nothing useful to add. Counting those would leave the project
# reporting work to do forever, however many times the button was pressed.
_WORTH_PUNCTUATING = 12


def _should_punctuate(text: str) -> bool:
    """The one gate both pending() and apply_all() consult.

    They must agree exactly. When only pending applied the length floor, the
    endpoint kept reporting a changed sentence that pending had never counted,
    so the badge said zero while the button still did something.
    """
    return (punctuation.available() and len(text) >= _WORTH_PUNCTUATING
            and punctuation.needs_punctuation(text))


def pending(project: dict, rules: list[dict] | None = None) -> dict[str, int]:
    """How many segments each pass would change. Nothing is modified.

    This runs on every project load, so it must not touch the punctuation
    model - 260 segments would mean 260 inferences per request. It predicts
    the punctuation pass from the same density gate the pass itself uses,
    which is the cheap half of that decision.
    """
    if rules is None:
        rules = vocabulary.rules_for(project)

    vocab = punct = split = 0
    for seg in _targets(project):
        text = seg.get("text", "")
        _, n = vocabulary.apply(text, rules)
        if n:
            vocab += 1
        if _should_punctuate(text):
            punct += 1
        # split_span, not split_text: split_span merges a piece too short to
        # deserve its own row, so predicting with split_text promised splits
        # that pressing the button would never deliver, and the badge could
        # never reach zero. It costs no model call, so it is free to be exact.
        if len(sentences.split_span(seg["start"], seg["end"], text)) > 1:
            split += 1

    return {"vocabulary": vocab, "punctuation": punct, "split": split,
            "total": vocab + punct + split}


def apply_all(project: dict, rules: list[dict] | None = None) -> dict[str, int]:
    """Run every text fix over `project["segments"]`, in place.

    Returns the same shape as `pending`, counting what actually changed.
    """
    if rules is None:
        rules = vocabulary.rules_for(project)

    vocab = punct = split = 0
    out: list[dict] = []

    for seg in project.get("segments", []):
        if seg.get("edited"):
            out.append(seg)
            continue

        text = seg.get("text", "")
        fixed, n = vocabulary.apply(text, rules)
        if n:
            vocab += 1
        after = (punctuation.restore_if_needed(fixed)
                 if _should_punctuate(fixed) else fixed)
        if after != fixed:
            punct += 1

        pieces = sentences.split_span(seg["start"], seg["end"], after)
        if len(pieces) <= 1:
            seg["text"] = after
            # The passes are corrections, not edits, so the "unedited" text
            # moves with them - otherwise set_zh_mode() would later re-convert
            # from a version that no longer exists.
            seg["original_text"] = after
            out.append(seg)
            continue

        split += 1
        for i, (start, end, piece) in enumerate(pieces):
            # The first piece keeps the original id so anything holding a
            # reference to it still resolves; the rest get a stable suffix.
            child = dict(seg)
            child["id"] = seg["id"] if i == 0 else f"{seg['id']}-s{i}"
            child["start"], child["end"] = start, end
            child["text"] = child["original_text"] = piece
            out.append(child)

    out.sort(key=lambda s: s["start"])
    project["segments"] = out

    counts = {"vocabulary": vocab, "punctuation": punct, "split": split,
              "total": vocab + punct + split}
    if counts["total"]:
        log.info("整理逐字稿：詞語 %d 句、標點 %d 句、切句 %d 句",
                 vocab, punct, split)
    return counts
