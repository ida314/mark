"""Deterministic discourse cues that hint a correction is underway.

A cue is a cheap, local signal — no LLM, no DB — that the user is correcting or updating
something already believed. It may only do two things: raise *synchronicity* (let
`memory_remember` adjudicate inline instead of queuing) and *hint* a relation to the judge
in `review.process_candidate`. It must never bypass the gate, never auto-retract a fact,
and never raise the proposer's trust level. The gate still decides; the cue only decides
what gets compared, and how fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Relation = Literal["corrects", "updates", "refines"]

# Leading punctuation/quoting stripped before checking the anchored cues, so a cue only
# fires when it actually opens the statement — "I was going to say actually yes" must not.
_LEADING_STRIP = re.compile(r"""^[\s"'.,;:!?-]+""")

_LEADING_CUES: tuple[tuple[re.Pattern[str], Relation], ...] = (
    (re.compile(r"^actually\b", re.I), "corrects"),
    (re.compile(r"^no\s*,", re.I), "corrects"),
)

_ANYWHERE_CUES: tuple[tuple[re.Pattern[str], Relation], ...] = (
    (re.compile(r"\bthat'?s wrong\b", re.I), "corrects"),
    (re.compile(r"\bi moved\b", re.I), "updates"),
    (re.compile(r"\bi no longer\b", re.I), "updates"),
)

_NOT_X_IS_Y = re.compile(r"\bit'?s not\s+(?P<x>.+?),\s*it'?s\s+(?P<y>.+)", re.I)


@dataclass(frozen=True)
class CorrectionCue:
    relation: Relation
    matched: str
    replaces: str | None = None
    replacement: str | None = None


def detect_correction(text: str) -> CorrectionCue | None:
    """Return a `CorrectionCue` if `text` opens with, or contains, a known correction cue.

    Order matters: the "it's not X, it's Y" shape is checked first because it carries the
    most useful hint (what is being replaced with what); then the leading cues, anchored to
    the start of the statement; then the anywhere cues, which name their own shift in
    meaning ("I moved", "I no longer") regardless of position.
    """
    match = _NOT_X_IS_Y.search(text)
    if match:
        return CorrectionCue(
            relation="corrects",
            matched=match.group(0),
            replaces=match.group("x").strip(),
            replacement=match.group("y").strip(),
        )

    stripped = _LEADING_STRIP.sub("", text)
    for pattern, relation in _LEADING_CUES:
        leading = pattern.match(stripped)
        if leading:
            return CorrectionCue(relation=relation, matched=leading.group(0))

    for pattern, relation in _ANYWHERE_CUES:
        anywhere = pattern.search(text)
        if anywhere:
            return CorrectionCue(relation=relation, matched=anywhere.group(0))

    return None


__all__ = ["CorrectionCue", "Relation", "detect_correction"]
