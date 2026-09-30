"""Cut a segment into the sentences inside it.

A segment is a VAD chunk intersected with a diarization turn, which means it
is however much someone said between two pauses - often a whole paragraph.
Measured on a real 25-minute meeting: 41 of 118 segments ran past 80
characters, the longest 357 characters across 60 seconds.

That undercuts the point of the whole pipeline. Diarization and ASR are
intersected so that a sentence can be handed back to the right speaker, but
you cannot reassign the third sentence of a paragraph when the paragraph is
one row. Splitting also makes the SRT export usable, since a subtitle cue
stops being a wall of text.

This runs *after* punctuation, never before: the terminal marks are what say
where the sentences are, and on the meeting above only 12 of 118 segments
carried an internal one until the punctuation pass had run.
"""
from __future__ import annotations

import re

from . import config as C

# Full stops, question and exclamation marks, both widths. A half-width "."
# is deliberately not here: in a technical meeting it is a decimal point
# ("3.5 mm") far more often than a full stop, and the punctuation model
# writes "。" anyway, so including it would only ever cost us.
_TERMINAL = "。！？!?"

# Clause marks. Only used to rescue a sentence that is still far too long:
# the punctuation model writes commas much more freely than full stops, so on
# a real meeting splitting on terminal marks alone still left 24 rows past 80
# characters, one of them 230.
_SECONDARY = "，、；：,;:"

# Zero-width split *after* a mark, so the mark stays on its own piece.
_SPLIT = re.compile(r"(?<=[" + re.escape(_TERMINAL) + r"])")
_SPLIT_CLAUSE = re.compile(r"(?<=[" + re.escape(_SECONDARY) + r"])")


def _break_long(part: str, max_chars: int) -> list[str]:
    """Cut an over-long sentence at clause boundaries, greedily."""
    if len(part) <= max_chars:
        return [part]
    clauses = [c for c in _SPLIT_CLAUSE.split(part) if c]
    if len(clauses) <= 1:
        # Nothing to cut on. A mid-word cut reads worse than a long row, so
        # this one stays long.
        return [part]

    out: list[str] = []
    buf = ""
    for clause in clauses:
        if buf and len(buf) + len(clause) > max_chars:
            out.append(buf)
            buf = clause
        else:
            buf += clause
    if buf:
        out.append(buf)
    return out


def split_text(text: str, max_chars: int | None = None) -> list[str]:
    """Split one line into sentences, each keeping its terminal mark.

    With `max_chars`, any sentence still longer than that is further cut at
    clause boundaries - a worse split point, but far better than a row nobody
    can read or reassign.
    """
    if not text:
        return []
    parts = [part for part in _SPLIT.split(text) if part]
    if max_chars is None:
        return parts
    out: list[str] = []
    for part in parts:
        out.extend(_break_long(part, max_chars))
    return out


def split_with_timestamps(start: float, end: float, tokens: list[str],
                          timestamps: list[float],
                          min_sec: float | None = None,
                          max_chars: int | None = -1
                          ) -> list[tuple[float, float, str]]:
    """Split at sentence boundaries using the model's own token times.

    `split_span()` divides the span by character count, which quietly assumes
    every character took the same time to say. Measured against this
    transducer's timestamps over 85 real sentence cuts: median error 0.38s,
    p90 1.31s, 63% of cuts wrong by more than 0.3s and 14% by over a second.
    That is audible - the per-sentence play button starts mid-word - and
    visibly out of sync in an SRT.

    `tokens` and `timestamps` come straight off `stream.result`; the times are
    offsets into the decoded clip, so they are added to `start`. Falls back to
    the character-count estimate whenever the two lists do not line up, which
    is the case for any model that does not return timestamps.
    """
    if min_sec is None:
        min_sec = C.MIN_SEGMENT_SEC
    if max_chars == -1:
        max_chars = C.SENTENCE_MAX_CHARS

    text = "".join(t.strip() for t in tokens)
    if not tokens or len(tokens) != len(timestamps) or not text:
        return split_span(start, end, text, min_sec, max_chars)

    parts = split_text(text, max_chars)
    if len(parts) <= 1:
        return [(start, end, text)]

    # Character offset where each token begins, so a cut in the text can be
    # traced back to the token that produced it.
    tok_start, acc = [], 0
    for t in tokens:
        tok_start.append(acc)
        acc += len(t.strip())

    def time_at(char_index: int) -> float:
        """Absolute time of the token containing this character."""
        k = 0
        for j, s in enumerate(tok_start):
            if s <= char_index:
                k = j
            else:
                break
        return start + float(timestamps[k])

    out: list[tuple[float, float, str]] = []
    cursor, at = start, 0
    for i, part in enumerate(parts):
        at += len(part)
        stop = end if i == len(parts) - 1 else time_at(at)
        # Times come from a model, so do not trust them to be ordered or to
        # stay inside the segment.
        stop = min(max(stop, cursor), end)
        out.append((cursor, stop, part))
        cursor = stop

    # Fold away anything too short to be its own row, exactly as split_span
    # does, so both paths produce rows of the same minimum size.
    merged: list[tuple[float, float, str]] = []
    for piece in out:
        if merged and piece[1] - piece[0] < min_sec:
            a, _, txt = merged[-1]
            merged[-1] = (a, piece[1], txt + piece[2])
        else:
            merged.append(piece)
    while len(merged) > 1 and merged[0][1] - merged[0][0] < min_sec:
        a, _, t1 = merged[0]
        _, b, t2 = merged.pop(1)
        merged[0] = (a, b, t1 + t2)
    return merged


def split_span(start: float, end: float, text: str,
               min_sec: float | None = None,
               max_chars: int | None = -1) -> list[tuple[float, float, str]]:
    """Apportion [start, end] across the sentences in `text`.

    Time is divided by character count, which is the only signal available
    without re-running the recogniser - good enough to seek by, and the
    sentence boundaries themselves are exact.

    The first piece starts at `start` and the last ends at `end`, so the
    original span is covered with no gap and no overlap.
    """
    if min_sec is None:
        min_sec = C.MIN_SEGMENT_SEC
    if max_chars == -1:                      # -1 means "use the configured cap"
        max_chars = C.SENTENCE_MAX_CHARS

    parts = split_text(text, max_chars)
    if len(parts) <= 1:
        return [(start, end, text)]

    span = end - start
    total = sum(len(p) for p in parts)
    if span <= 0 or total == 0:
        return [(start, end, text)]

    # A one-character sentence in a long segment would be allotted a few
    # milliseconds, which is not a thing anyone can click on. Fold those into
    # the sentence before them - or, for a short opener, the one after.
    merged: list[str] = []
    for part in parts:
        if merged and len(part) / total * span < min_sec:
            merged[-1] += part
        else:
            merged.append(part)
    while len(merged) > 1 and len(merged[0]) / total * span < min_sec:
        merged[0] += merged.pop(1)

    out: list[tuple[float, float, str]] = []
    cursor, used = start, 0
    kept = sum(len(p) for p in merged)
    for i, part in enumerate(merged):
        used += len(part)
        # Pin the final boundary rather than accumulating rounding error.
        stop = end if i == len(merged) - 1 else start + span * used / kept
        out.append((cursor, stop, part))
        cursor = stop
    return out
