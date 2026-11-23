"""Deterministic reply classification. Deliberately NOT an LLM call: opt-out handling is a legal
requirement and must be predictable, auditable and instant."""
from __future__ import annotations

import re
import unicodedata

_STOP = re.compile(
    r"^\s*(stop|stopall|unsubscribe|quit|end|opt[\s-]*out)\s*[.!]*\s*$"
    r"|\bunsubscribe\b|\bstop\s+(messag|text|send|contact|remind)|\bdon'?t\s+(message|contact|text)",
    re.I,
)
_RESCHEDULE = re.compile(
    r"\b(re-?schedule|postpone|change|cancel|different\s+(time|day|date)|another\s+(time|day|date)"
    r"|can'?t|cannot|unable|not\s+able)\b|^\s*(no|nope|nahi|nahin)\s*[.!]*\s*$",
    re.I,
)
_CONFIRM = re.compile(
    r"\b(yes|y|yep|yeah|yup|confirm|confirmed|ok|okay|sure|will\s+be\s+there|haan|han|theek|thik)\b", re.I
)


def classify_intent(text: str | None) -> str:
    """Returns one of: stop | reschedule | confirm | other. Priority: stop > reschedule > confirm."""
    t = unicodedata.normalize("NFKC", text or "").replace("\u2019", "'").strip()
    if not t:
        return "other"
    if _STOP.search(t):
        return "stop"
    if _RESCHEDULE.search(t):
        return "reschedule"
    if _CONFIRM.search(t) or "\U0001F44D" in t:
        return "confirm"
    return "other"
