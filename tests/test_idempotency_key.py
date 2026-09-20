"""Two attempts at one call have to hash alike, and two different calls never may.

This is the pass's stated exit criterion and the part of it that fails silently. Nothing
here raises when it goes wrong: a key that drifts between attempts makes a resume re-send
the mail, and a key that collides across calls makes a resume suppress an action that never
ran and report it as done. Both look exactly like a working system right up until the
crash, which is why the derivation is pinned here rather than eyeballed.

The asymmetry matters and drives most of the choices being asserted. A missed exclusion
costs a duplicate action, recoverable and visible. A wrong exclusion - dropping a field
that carried meaning - costs an action that never happens and is reported as already done,
which nobody ever finds. So exclusion is by exact top-level name, never by pattern, and
never for a name the tool itself declares as a parameter.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from agentd.tools.idempotency import (
    EXCLUDED_ARGS,
    EXCLUSION_REASONS,
    CanonicalizationError,
    args_hash,
    canonical_args,
    declared_names,
    excluded_from,
    idempotency_key,
)
from agentd.tools.registry import build_registry

# The exclusion list, written out rather than derived, so that adding or removing a name is
# a change somebody has to make twice and justify once. Every name here is a field the
# *caller regenerates per attempt*; anything not here is part of the action and is hashed.
EXCLUDED_BY_DESIGN = {
    "reason",
    "idempotency_key",
    "request_id",
    "client_request_id",
    "client_token",
    "nonce",
    "attempt",
    "retry",
    "retry_count",
    "now",
    "timestamp",
    "requested_at",
    "sent_at",
    "call_id",
    "action_id",
    "trace_id",
    "span_id",
    "parent_span_id",
}


def key(args: dict, *, run_id: str = "run-1", step_id: str = "s1", tool: str = "gmail_send",
        declared: tuple[str, ...] = ()) -> str:
    return idempotency_key(
        run_id=run_id, step_id=step_id, tool_name=tool, args=args, declared=declared
    )


# --- the exclusion list ------------------------------------------------------


def test_every_field_excluded_from_the_key_is_named_and_justified() -> None:
    """The list is the specification. A name with no reason is a name nobody argued for."""
    assert set(EXCLUDED_ARGS) == EXCLUDED_BY_DESIGN
    assert set(EXCLUSION_REASONS) == set(EXCLUDED_ARGS)
    assert all(reason.strip() for reason in EXCLUSION_REASONS.values())


def test_two_attempts_at_the_same_logical_call_produce_the_same_key() -> None:
    """The exit criterion, with every difference an attempt is allowed to have.

    The model rewrote its justification, the caller minted a fresh request id, the clock
    moved, and the arguments came back in a different order out of a different JSON decode.
    None of that is the call.
    """
    first = key(
        {
            "to": "dyd2008@nyu.edu",
            "subject": "rent",
            "body": "sending it tonight",
            "reason": "the user asked me to confirm",
            "request_id": "req-0001",
            "timestamp": "2026-09-20T18:00:00Z",
        }
    )
    second = key(
        {
            "body": "sending it tonight",
            "subject": "rent",
            "to": "dyd2008@nyu.edu",
            "reason": "confirming, as requested",
            "request_id": "req-0002",
            "timestamp": "2026-09-20T18:00:04Z",
        }
    )
    assert first == second


def test_changing_anything_the_call_actually_does_changes_the_key() -> None:
    """The other half. A key that survives a change of recipient is a key that would
    suppress a second, different email as a duplicate of the first."""
    base = {"to": "a@example.com", "subject": "rent", "body": "tonight"}
    assert key(base) != key({**base, "to": "b@example.com"})
    assert key(base) != key({**base, "body": "tomorrow"})
    assert key(base) != key({**base, "cc": "c@example.com"})
    assert key(base) != key(base, tool="gmail_draft")
    assert key(base) != key(base, step_id="s2")
    assert key(base) != key(base, run_id="run-2")


def test_a_field_the_tool_declares_is_part_of_the_call_even_if_it_is_on_the_list() -> None:
    """The exclusion list is generic; a tool's schema is specific, and it wins.

    A tool whose parameter really is called `timestamp` is a tool for which two calls with
    two timestamps are two different actions. Dropping it would make them one.
    """
    a = {"timestamp": "2026-09-20T18:00:00Z", "event": "standup"}
    b = {"timestamp": "2026-09-21T18:00:00Z", "event": "standup"}
    assert key(a) == key(b)  # undeclared: metadata, dropped
    assert key(a, declared=("timestamp", "event")) != key(b, declared=("timestamp", "event"))


def test_no_registered_tool_declares_a_parameter_the_key_would_drop() -> None:
    """A canary, not a rule. The `declared` guard above means such a tool is handled
    correctly - but it is worth knowing the first time one appears, because from then on
    the exclusion list and a tool's schema disagree about what that name means."""
    offenders = {
        t.name: sorted(set(declared_names(t.parameters)) & EXCLUDED_ARGS)
        for t in build_registry().tools.values()
    }
    assert {name: names for name, names in offenders.items() if names} == {}


def test_exclusion_applies_to_the_top_level_only() -> None:
    """A `timestamp` inside a payload the user wrote is content, not plumbing - and
    stripping it would silently rewrite what the call does."""
    a = {"payload": {"timestamp": "2026-09-20T18:00:00Z"}}
    b = {"payload": {"timestamp": "2026-09-21T18:00:00Z"}}
    assert key(a) != key(b)
    assert excluded_from(a) == []


def test_which_fields_a_call_drops_can_be_asked_rather_than_inferred() -> None:
    args = {"to": "a@example.com", "reason": "asked", "nonce": "x", "when": "tomorrow"}
    assert excluded_from(args) == ["nonce", "reason"]
    assert excluded_from(args, declared=("reason",)) == ["nonce"]


# --- canonicalization --------------------------------------------------------


def test_the_canonical_form_normalizes_structure_and_nothing_else() -> None:
    """Key order and tuple-versus-list are ours to normalize. The characters in a string
    are the caller's meaning: case folding or trimming here would merge two calls the user
    sees as different."""
    assert canonical_args({"b": [1, {"y": 2, "x": 1}], "a": (3, 4)}) == (
        '{"a":[3,4],"b":[1,{"x":1,"y":2}]}'
    )
    assert key({"subject": "Rent"}) != key({"subject": "rent"})
    assert key({"subject": "rent "}) != key({"subject": "rent"})
    assert key({"subject": "résumé"}) != key({"subject": "résumé"})


def test_order_within_a_list_is_meaning_and_survives() -> None:
    assert key({"paths": ["a", "b"]}) != key({"paths": ["b", "a"]})


def test_a_value_with_no_canonical_form_is_refused_rather_than_stringified() -> None:
    """`repo_ops.args_hash` hashes with `default=str`. For most objects that is a repr
    containing a memory address, so the "same" call hashes differently on every attempt -
    a key that looks healthy and never matches. Here it raises."""
    with pytest.raises(CanonicalizationError) as exc:
        key({"when": datetime(2026, 9, 20)})
    assert "when" in str(exc.value) and "datetime" in str(exc.value)

    with pytest.raises(CanonicalizationError) as exc:
        key({"payload": {"inner": object()}})
    assert "args.payload.inner" in str(exc.value)


def test_a_non_string_object_key_is_refused_because_json_would_merge_it() -> None:
    """`json.dumps` turns {1: "a"} into {"1": "a"} without a word, so two different
    argument objects would share one key."""
    with pytest.raises(CanonicalizationError):
        canonical_args({1: "a"})


def test_a_value_that_is_not_a_number_is_refused() -> None:
    """NaN is not equal to itself, so a key containing one means nothing."""
    with pytest.raises(CanonicalizationError):
        canonical_args({"amount": float("nan")})
    with pytest.raises(CanonicalizationError):
        canonical_args({"amount": float("inf")})


def test_a_key_cannot_be_derived_without_a_run_and_a_step() -> None:
    """The collision 3a flagged: every key with the same hole in it is the same key, so
    every detached call would look like a retry of every other one."""
    with pytest.raises(CanonicalizationError):
        idempotency_key(run_id="", step_id="s1", tool_name="fs_write", args={})
    with pytest.raises(CanonicalizationError):
        idempotency_key(run_id="run-1", step_id="", tool_name="fs_write", args={})


def test_no_part_of_the_key_can_impersonate_another() -> None:
    """The parts are hashed as a document, not joined with a separator the caller could
    also type. A run named "a" at step "b|c" is not a run named "a|b" at step "c"."""
    assert key({}, run_id="a", step_id="b|c") != key({}, run_id="a|b", step_id="c")
    assert key({"a": "1", "b": "2"}) != key({"a": '1","b":"2'})


def test_the_derivation_itself_is_pinned() -> None:
    """A golden value. Changing how the key is derived is allowed - silently changing it is
    not, because every key already on disk would stop matching and every one of those calls
    would read as "never happened"."""
    assert key(
        {"path": "/tmp/a.txt", "content": "hello", "reason": "because"},
        tool="fs_write",
    ) == "0541f4c6bfdfc03e109fc08aeaecf77465dcda7c579c1d787e832227ebe5d969"


def test_the_args_hash_answers_a_different_question_than_the_key() -> None:
    """Same arguments in two different runs: different effects, identical arguments. The
    human reconciling an uncertain effect compares the second."""
    args = {"to": "a@example.com"}
    assert key(args, run_id="run-1") != key(args, run_id="run-2")
    assert args_hash(args) == args_hash({"to": "a@example.com", "reason": "whatever"})
