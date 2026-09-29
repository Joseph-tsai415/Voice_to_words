"""A correction list for words the recogniser reliably gets wrong.

Every meeting has names, jargon and product terms that no general model knows -
"孟青" comes out as "夢晴", an internal system name turns into something that
merely sounds like it. Those mistakes repeat, so they are worth fixing once.

The list is applied automatically to new transcripts and can be re-applied to
an existing one after editing it, without re-running recognition. Corrections
are stored per project as well, so a term that only matters to one meeting does
not leak into the others.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from .config import DATA_DIR

log = logging.getLogger("scribe.vocabulary")

GLOBAL_PATH = DATA_DIR / "vocabulary.json"
_guard = threading.Lock()


def _normalise(entries: Any) -> list[dict[str, Any]]:
    """Accept a loose list and return well-formed, de-duplicated rules."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()
    for raw in entries or []:
        if isinstance(raw, str):                 # "wrong=right" shorthand
            wrong, _, right = raw.partition("=")
            raw = {"wrong": wrong, "right": right}
        if not isinstance(raw, dict):
            continue
        wrong = str(raw.get("wrong", "")).strip()
        right = str(raw.get("right", "")).strip()
        if not wrong or wrong == right:
            continue
        whole = bool(raw.get("whole_word", False))
        key = (wrong, whole)
        if key in seen:
            continue
        seen.add(key)
        out.append({"wrong": wrong, "right": right, "whole_word": whole,
                    "note": str(raw.get("note", "")).strip()})
    # Longest first: fixing "外媒體公司" must win over a rule for "外媒體".
    out.sort(key=lambda r: len(r["wrong"]), reverse=True)
    return out


def load_global() -> list[dict[str, Any]]:
    with _guard:
        try:
            with GLOBAL_PATH.open(encoding="utf-8") as fh:
                return _normalise(json.load(fh))
        except (OSError, json.JSONDecodeError):
            return []


def save_global(entries: Any) -> list[dict[str, Any]]:
    rules = _normalise(entries)
    with _guard:
        GLOBAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = GLOBAL_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(rules, fh, ensure_ascii=False, indent=2)
        tmp.replace(GLOBAL_PATH)
    log.info("更新詞語修正表：%d 條", len(rules))
    return rules


def rules_for(project: dict | None = None) -> list[dict[str, Any]]:
    """Global rules plus anything specific to this project."""
    rules = load_global()
    if project:
        rules = _normalise(list(project.get("vocabulary") or []) + rules)
    return rules


def _pattern(rule: dict[str, Any]) -> re.Pattern:
    wrong = re.escape(rule["wrong"])
    if rule.get("whole_word") and re.match(r"^[\w]+$", rule["wrong"]):
        # Word boundaries only mean something for Latin text; CJK has no spaces.
        return re.compile(rf"\b{wrong}\b")
    return re.compile(wrong)


def apply(text: str, rules: list[dict[str, Any]]) -> tuple[str, int]:
    """Return (corrected text, number of replacements)."""
    if not text or not rules:
        return text, 0
    total = 0
    for rule in rules:
        text, n = _pattern(rule).subn(rule["right"], text)
        total += n
    return text, total


def apply_to_segments(segments: list[dict], rules: list[dict[str, Any]]
                      ) -> tuple[int, int]:
    """Correct every segment in place. Returns (sentences changed, replacements)."""
    changed = replacements = 0
    for seg in segments:
        fixed, n = apply(seg.get("text", ""), rules)
        if n:
            seg["text"] = fixed
            seg["corrected"] = True
            changed += 1
            replacements += n
    return changed, replacements
