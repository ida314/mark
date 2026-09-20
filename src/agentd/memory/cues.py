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
    # "<something> is wrong, i live in the east village" — the shape that failed live.
    # Three guards, because this one runs on every turn a developer types in their own repo,
    # where "the test is wrong", "the CI config is wrong, fix it" and "the migration is
    # wrong" are ordinary chatter:
    #
    # - anchored, because naming an error the statement then discusses is not a correction;
    # - clause-final ("wrong" then a boundary), because a correction stops at "wrong" and
    #   states the truth, while a complaint keeps qualifying it ("wrong about the ordering");
    # - followed by a **first-person restatement**, which is the signal that actually
    #   separates the two. Enumerating which nouns are memory-ish ("address" yes, "CI config"
    #   no) would never generalise; "and here is what is true instead, about me" does.
    #
    # `i\b` covers "i live", "i'm" and "i am". A bare "your info is wrong" with nothing
    # stated to replace the belief deliberately stops firing: there is nothing for the fast
    # path to adjudicate, so it belongs in the queue.
    (
        re.compile(
            r"^(?!(?:i think|i believe|i guess|i suspect|maybe|perhaps|tell me|if)\b)"
            r"[^,.;!?]{1,60}?\s+(?:is|are|was|were)\s+wrong\b"
            r"(?=\s*[,.;!]\s*(?:i\b|my\b|it'?s\b))",
            re.I,
        ),
        "corrects",
    ),
)

_ANYWHERE_CUES: tuple[tuple[re.Pattern[str], Relation], ...] = (
    (re.compile(r"\bthat'?s wrong\b", re.I), "corrects"),
    # "I moved" / "I've moved" / "I have moved" — one shape, three spellings.
    (re.compile(r"\bi(?:'ve| have)? moved\b", re.I), "updates"),
    (re.compile(r"\bi no longer\b", re.I), "updates"),
    # Denying a stored claim outright ("I don't live in Brooklyn") says it was never true,
    # so it corrects rather than updates; a world-change says so in its own words above.
    (
        re.compile(r"\bi\s+(?:don'?t|do not)\s+(?:live|work|study|use|own|go)\b", re.I),
        "corrects",
    ),
)

# "it's not X, it's Y" and its first-person sibling "(I'm) not X, I'm Y". Both name what is
# being replaced with what, which is the most useful hint a cue can carry. The hedges are
# excluded because "I'm not sure, I'm going to check" is the same shape and corrects nothing.
_REPLACEMENT_CUES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bit'?s not\s+(?P<x>.+?),\s*it'?s\s+(?P<y>.+)", re.I),
    re.compile(
        r"\b(?:i'?m\s+)?not\s+(?!sure\b|certain\b|positive\b)(?P<x>.+?),\s*i'?m\s+(?P<y>.+)",
        re.I,
    ),
)


@dataclass(frozen=True)
class CorrectionCue:
    relation: Relation
    matched: str
    replaces: str | None = None
    replacement: str | None = None


def detect_correction(text: str) -> CorrectionCue | None:
    """Return a `CorrectionCue` if `text` opens with, or contains, a known correction cue.

    Order matters: the "not X, it's/I'm Y" shapes are checked first because they carry the
    most useful hint (what is being replaced with what); then the leading cues, anchored to
    the start of the statement; then the anywhere cues, which name their own shift in
    meaning ("I moved", "I no longer") regardless of position.
    """
    for pattern in _REPLACEMENT_CUES:
        match = pattern.search(text)
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
