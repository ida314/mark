"""Effect classes: whether re-running a tool call is safe.

After a crash the runtime has exactly one question to answer about a call it cannot prove
finished — "may I just do that again?" — and no amount of reasoning over the arguments
answers it. The tool has to have said so in advance:

    read              no external state change; free to re-execute
    idempotent_write  re-execution converges to the same state
    unsafe_write      re-execution may duplicate a real-world action

`effect_class` is mandatory on every tool and has **no default**. A tool that arrives
without one fails registration rather than falling back to `read`, because a silent
fallback is precisely the failure that sends the email twice: the unlabelled tool is by
definition the one nobody thought about, and the resume path would read that silence as a
promise that re-running is free.

Session 3b writes the ledger keyed by these values; Pass 4 reconciles against them.
This module is the vocabulary, the gate, and the list of tools still awaiting a ruling.
"""

from __future__ import annotations

EffectClass = str  # read | idempotent_write | unsafe_write

READ: EffectClass = "read"
IDEMPOTENT_WRITE: EffectClass = "idempotent_write"
UNSAFE_WRITE: EffectClass = "unsafe_write"

EFFECT_CLASSES: tuple[EffectClass, ...] = (READ, IDEMPOTENT_WRITE, UNSAFE_WRITE)

# The classes that get a ledger row and a pair of `effect_*` journal events (session 3b).
# `read` is excluded because there is no question for a crash to leave behind: nothing
# outside changed, so re-executing is free and there is nothing to reconcile. Defined as
# the complement of `read` rather than as a second hand-written list, so a fourth class
# could never be added to one and forgotten in the other.
EFFECTING: tuple[EffectClass, ...] = tuple(c for c in EFFECT_CLASSES if c != READ)

MEANING: dict[EffectClass, str] = {
    READ: "no external state change; free to re-execute",
    IDEMPOTENT_WRITE: "re-execution converges to the same state",
    UNSAFE_WRITE: "re-execution may duplicate a real-world action",
}

# The value a tool carries when nobody has judged it yet.
#
# It *is* `unsafe_write` - not a fourth class, and it passes the same gate - because the
# rule for an unclassified effect is the conservative one: a wrong `unsafe_write` costs one
# avoidable prompt, a wrong `read` costs a duplicate action that cannot be taken back.
# It is spelled differently at the call site so the declaration says "nobody has looked at
# this" rather than "somebody decided this is unsafe", which are not the same claim and
# would otherwise be indistinguishable in the source.
#
# Session 3c ruled on nineteen of these and 3d on the last six, dropping each name from
# UNAUDITED_TOOLS below as it was ruled on. The last name, `memory_search`, was a deferral
# to a human rather than an omission, and was ruled `read` at the Pass 3/4 boundary. The
# placeholder stays: it is what a *new* tool declares before anybody has looked at it.
UNAUDITED: EffectClass = UNSAFE_WRITE

# Which tools are in that state, as data rather than as a paragraph that goes stale. A
# name in here means: registered, declaring `unsafe_write`, and carrying no recorded
# ruling. `docs/records/effect-classification.md` says why, for every name.
#
# This set is empty, so the audit is done: every registered tool carries a recorded
# ruling. It stays as a declared, tested slot rather than being deleted, because a *new*
# tool added with `UNAUDITED` belongs in here and `tests/test_tool_effect_class.py` will
# insist on it. Emptiness is the invariant, not the absence of the mechanism.
#
# The last entry was `memory_search`, held here through 3c and 3d as "somebody looked and
# would not decide alone" rather than "nobody has looked". It was ruled `read` at the
# Pass 3/4 boundary as a deliberate exception - the counter it moves is bookkeeping about
# accesses, not state correctness depends on. The reasoning is at its declaration in
# `builtin_memory.py` and in `docs/records/effect-classification.md`.
UNAUDITED_TOOLS: frozenset[str] = frozenset()


class ToolRegistrationError(Exception):
    """A tool was refused at registration and is not callable.

    Loud on purpose: the alternative to a refused tool is a tool whose effect class the
    runtime has to guess at the moment it least can afford to.
    """


def check_effect_class(name: str, value: object) -> EffectClass:
    """Return `value` if it is a declared effect class, else refuse the tool.

    Takes the name rather than the tool so this module stays importable from `base`, and
    takes `object` rather than `str` because the case worth catching is the one where the
    attribute is missing, `None`, or something that is not a string at all.
    """
    if not isinstance(value, str) or not value:
        raise ToolRegistrationError(
            f"tool {name!r} declares no effect_class. It is mandatory and has no default: "
            f"say which of {', '.join(EFFECT_CLASSES)} applies. "
            f"When in doubt, {UNSAFE_WRITE} - "
            f"{MEANING[UNSAFE_WRITE]}, and being wrong the other way duplicates it."
        )
    if value not in EFFECT_CLASSES:
        raise ToolRegistrationError(
            f"tool {name!r} declares effect_class={value!r}, which is not one of "
            f"{', '.join(EFFECT_CLASSES)}."
        )
    return value
