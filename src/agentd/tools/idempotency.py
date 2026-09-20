"""The idempotency key: what makes two attempts at the same call one effect.

```
idempotency_key = hash(run_id, step_id, tool_name, canonical_args)
```

The key is the only thing that will let Pass 4 say "this already happened" about a call it
cannot prove finished. Everything here exists to make one property true:

    the same logical call, attempted twice, produces the same key -
    and two different calls never do.

Both halves fail silently. A key that drifts between attempts makes resume re-send the
mail; a key that collides across calls makes resume suppress an action that never ran and
report it as done. Neither raises anything. That is why canonicalization is written out
here as rules rather than as a `json.dumps` call somebody will later "simplify".

## What is hashed

The run, the step, the tool name, and the arguments after canonicalization. Not the effect
class (a tool's class can be corrected by an audit without renaming every effect it ever
had), not the result, not the time.

## Canonicalization normalizes structure, never content

Dict keys are sorted at every depth, tuples become lists, and the encoding is fixed. What
is *not* done is as deliberate: no Unicode normalization, no whitespace stripping, no case
folding, no number coercion. Those all make two *different* strings hash alike, and a false
"same call" is the more expensive of the two mistakes - it suppresses an action and tells
the user it happened. Structure is ours to normalize; content is the caller's meaning.

## Failure is loud

`repo_ops.args_hash` hashes with `default=str`, which turns an object with no JSON form
into its `repr` - including, for most objects, a memory address that differs on every
attempt. That is a key that looks perfectly healthy and silently never matches. Here a
value that cannot be canonically encoded raises `CanonicalizationError` naming the path and
the type, and the executor refuses the call rather than running an unsafe write under a key
that cannot be reproduced.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

# Bumped if the derivation ever changes. It is part of the hashed payload, so keys from an
# older build can never be mistaken for keys from this one - a silent change of derivation
# would otherwise look exactly like "this call never happened before".
KEY_VERSION = "effect/v1"


class CanonicalizationError(ValueError):
    """An argument value has no canonical form, so no stable key can be derived.

    Raised, never defaulted around. A call whose key cannot be reproduced cannot be
    deduplicated after a crash, and an `unsafe_write` in that state is the duplicate-action
    bug this pass exists to prevent.
    """


# --- the exclusion list ------------------------------------------------------
#
# Every name here, and why. A field is excluded when the *caller regenerates it per
# attempt*: it describes this try at the call rather than the call. Anything else - and
# anything not named here - is part of the action and is hashed.
#
# The list is kept as data with its reasons attached so that adding a name is an argued
# change rather than a one-word diff, and so a test can print it.
#
# The hazard runs in both directions and they are not symmetric. Forgetting to exclude a
# per-attempt field means two attempts at one call get two keys: a duplicate action after a
# crash. Excluding a field that carries *meaning* means two different calls share one key:
# an action silently suppressed and reported as already done. The second is worse, so
# exclusion is by exact top-level name only, and only when the tool itself does not declare
# that name as a parameter (see `canonical_args`).
EXCLUSION_REASONS: dict[str, str] = {
    # The justification the policy engine requires on every write. The executor pops it
    # before the handler ever sees it, and the model rephrases it freely between attempts;
    # it is a sentence for `agent why`, not an input to the action.
    "reason": "human justification, rewritten per attempt, never part of the action",
    # Caller-minted identifiers for the attempt. These are the textbook case: their whole
    # purpose is to be different every time.
    "idempotency_key": "the caller's own per-attempt key",
    "request_id": "per-attempt request identifier",
    "client_request_id": "per-attempt request identifier",
    "client_token": "per-attempt request token",
    "nonce": "per-attempt nonce",
    # Counters of the attempt rather than of the call.
    "attempt": "counts the attempt, not the call",
    "retry": "counts the attempt, not the call",
    "retry_count": "counts the attempt, not the call",
    # Wall clock read at the moment of the attempt. Note these are the *generated* kind:
    # `when`, `due_at`, `start`, `end` and friends are the user's intent and are hashed.
    "now": "wall clock at the moment of the attempt",
    "timestamp": "wall clock at the moment of the attempt",
    "requested_at": "wall clock at the moment of the attempt",
    "sent_at": "wall clock at the moment of the attempt",
    # Plumbing that identifies this attempt's context rather than the work.
    "call_id": "the provider's id for this tool call message",
    "action_id": "the id of this attempt's audit row",
    "trace_id": "observability context for this attempt",
    "span_id": "observability context for this attempt",
    "parent_span_id": "observability context for this attempt",
}

EXCLUDED_ARGS: frozenset[str] = frozenset(EXCLUSION_REASONS)


def canonical_value(value: Any, *, path: str = "args") -> Any:
    """`value` with every structural choice removed and every ambiguity refused.

    Dicts are rebuilt with sorted string keys, sequences keep their order (order is
    meaning), and scalars pass through. Anything else raises rather than being coerced.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                # json.dumps would quietly stringify this, so {1: "a"} and {"1": "a"} -
                # two different objects - would hash identically.
                raise CanonicalizationError(
                    f"{path}: object key {key!r} is {type(key).__name__}, not a string"
                )
            out[key] = canonical_value(value[key], path=f"{path}.{key}")
        return {k: out[k] for k in sorted(out)}
    if isinstance(value, (list, tuple)):
        return [canonical_value(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
    raise CanonicalizationError(
        f"{path} is {type(value).__name__}, which has no canonical JSON form. "
        "Pass a JSON value; an idempotency key derived from repr() would differ on "
        "every attempt."
    )


def _dumps(value: Any) -> str:
    try:
        # allow_nan=False: NaN is not equal to itself, so a key containing one is a key
        # that means nothing. Better to refuse the call than to derive it.
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ValueError as exc:  # out-of-range float, i.e. NaN or Infinity
        raise CanonicalizationError(f"argument is not finite: {exc}") from exc


def canonical_args(args: Mapping[str, Any], *, declared: Iterable[str] = ()) -> str:
    """The canonical JSON text of `args`, with the per-attempt fields dropped.

    `declared` is the tool's own parameter names. A name in `EXCLUDED_ARGS` that the tool
    declares as a real parameter is **kept**: the tool has said that field is part of the
    call, and dropping it would let two different calls share one key. Exclusion applies at
    the top level only - a `timestamp` nested inside a payload the user wrote is content.
    """
    if not isinstance(args, Mapping):
        raise CanonicalizationError(
            f"arguments are {type(args).__name__}, expected an object"
        )
    known = set(declared)
    kept = {
        name: value
        for name, value in args.items()
        if not (name in EXCLUDED_ARGS and name not in known)
    }
    return _dumps(canonical_value(kept))


def excluded_from(args: Mapping[str, Any], *, declared: Iterable[str] = ()) -> list[str]:
    """Which argument names this call would drop. For tests and for `agent why`."""
    known = set(declared)
    return sorted(n for n in args if n in EXCLUDED_ARGS and n not in known)


def args_hash(args: Mapping[str, Any], *, declared: Iterable[str] = ()) -> str:
    """The digest of the canonical arguments alone - the ledger's `args_hash`.

    Separate from the key because it answers a different question: the key says "is this
    the same call", `args_hash` says "were the arguments the same", which is what a human
    reconciling an uncertain effect wants to compare across runs.
    """
    return hashlib.sha256(canonical_args(args, declared=declared).encode()).hexdigest()


def idempotency_key(
    *,
    run_id: str,
    step_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    declared: Iterable[str] = (),
) -> str:
    """`hash(run_id, step_id, tool_name, canonical_args)`, as a 64-char hex digest.

    The four parts are hashed as a JSON array rather than joined with a separator, so no
    value can impersonate a boundary: `("a|b", "c")` and `("a", "b|c")` are different
    documents here and would be one string under any delimiter a caller could also type.

    `run_id` and `step_id` must be non-empty. A key derived from a missing run collides
    with every other key derived from a missing run, which is the one collision that would
    make every detached call look like a retry of every other one.
    """
    if not run_id or not step_id:
        raise CanonicalizationError(
            f"idempotency key needs a run and a step; got run_id={run_id!r} "
            f"step_id={step_id!r}. A key with a hole in it collides with every other "
            "key with the same hole."
        )
    if not tool_name:
        raise CanonicalizationError("idempotency key needs a tool name")
    payload = _dumps(
        [KEY_VERSION, run_id, step_id, tool_name, canonical_args(args, declared=declared)]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def declared_names(parameters: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The top-level parameter names out of a tool's JSON Schema."""
    properties = (parameters or {}).get("properties") or {}
    return tuple(properties) if isinstance(properties, Mapping) else ()


def digest(text: str) -> str:
    """A content digest for the journal's `args_digest` / `result_digest` fields."""
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = [
    "EXCLUDED_ARGS",
    "EXCLUSION_REASONS",
    "KEY_VERSION",
    "CanonicalizationError",
    "args_hash",
    "canonical_args",
    "canonical_value",
    "declared_names",
    "digest",
    "excluded_from",
    "idempotency_key",
]

