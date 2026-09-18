"""The policy engine decides what the model is allowed to do. It gets the most tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentd.policy.engine import PolicyContext, PolicyEngine, ToolCallInfo, cap_autonomy
from agentd.policy.model import Policy, PolicyError, load_policy

BASE_MATRIX = {
    "read": {"observe": "allow", "assist": "allow", "act": "allow", "trusted": "allow"},
    "draft": {"observe": "deny", "assist": "allow", "act": "allow", "trusted": "allow"},
    "write": {
        "observe": "deny", "assist": "require_approval", "act": "require_approval",
        "trusted": "allow",
    },
    "external": {
        "observe": "deny", "assist": "require_approval", "act": "require_approval",
        "trusted": "require_approval",
    },
    "destructive": {
        "observe": "deny", "assist": "require_approval", "act": "require_approval",
        "trusted": "require_approval",
    },
}


def engine(**overrides) -> PolicyEngine:
    return PolicyEngine(Policy.model_validate({"risk_matrix": BASE_MATRIX, **overrides}))


def call(name="t", risk="read", **kwargs) -> ToolCallInfo:
    return ToolCallInfo(name=name, risk=risk, **kwargs)


@pytest.mark.parametrize(
    ("risk", "autonomy", "expected"),
    [
        ("read", "observe", "allow"),
        ("draft", "observe", "deny"),
        ("draft", "assist", "allow"),
        ("write", "assist", "require_approval"),
        ("write", "observe", "deny"),
        ("external", "act", "require_approval"),
        ("destructive", "trusted", "require_approval"),
    ],
)
def test_risk_matrix_defaults(risk, autonomy, expected):
    decision = engine().evaluate(call(risk=risk), PolicyContext(autonomy=autonomy))
    assert decision.outcome == expected


def test_hard_deny_beats_an_allow_rule():
    e = engine(
        hard_deny=[{"id": "no-secrets", "match": {"args": {"path": {"glob": ["**/.env"]}}}}],
        rules=[{"id": "allow-all", "match": {}, "outcome": "allow"}],
    )
    decision = e.evaluate(
        call(risk="read", args={"path": "/home/x/.env"}), PolicyContext(autonomy="act")
    )
    assert decision.outcome == "deny"
    assert decision.rule_id == "no-secrets"


def test_first_matching_rule_wins():
    e = engine(
        rules=[
            {"id": "first", "match": {"tool": "t"}, "outcome": "allow"},
            {"id": "second", "match": {"tool": "t"}, "outcome": "deny"},
        ]
    )
    assert e.evaluate(call(risk="write"), PolicyContext()).rule_id == "first"


def test_path_escape_via_dotdot_is_caught(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    e = engine(
        hard_deny=[
            {
                "id": "outside-roots",
                "match": {"tags": ["fs"], "args": {"path": {"not_under": [str(allowed)]}}},
            }
        ]
    )
    escape = str(allowed / ".." / "secret.txt")
    decision = e.evaluate(
        call(risk="read", tags=("fs",), args={"path": escape}, path_args=("path",)),
        PolicyContext(),
    )
    assert decision.outcome == "deny"


def test_symlink_escape_is_caught(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("x")
    link = allowed / "link.txt"
    os.symlink(secret, link)
    e = engine(
        hard_deny=[
            {
                "id": "outside-roots",
                "match": {"tags": ["fs"], "args": {"path": {"not_under": [str(allowed)]}}},
            }
        ]
    )
    decision = e.evaluate(
        call(risk="read", tags=("fs",), args={"path": str(link)}, path_args=("path",)),
        PolicyContext(),
    )
    assert decision.outcome == "deny"


def test_taint_escalates_a_write_that_would_otherwise_be_allowed():
    e = engine(rules=[{"id": "auto", "match": {"tool": "fs_write"}, "outcome": "allow"}])
    clean = e.evaluate(call(name="fs_write", risk="write"), PolicyContext(autonomy="act"))
    tainted = e.evaluate(
        call(name="fs_write", risk="write"), PolicyContext(autonomy="act", tainted=True)
    )
    assert clean.outcome == "allow"
    assert tainted.outcome == "require_approval"


def test_taint_never_loosens_a_deny():
    e = engine(rules=[{"id": "no", "match": {"tool": "fs_write"}, "outcome": "deny"}])
    decision = e.evaluate(
        call(name="fs_write", risk="write"), PolicyContext(autonomy="act", tainted=True)
    )
    assert decision.outcome == "deny"


def test_daemon_origin_cannot_reach_external_tools():
    e = engine(
        rules=[
            {
                "id": "daemon-never-external",
                "match": {"origin": ["daemon"], "risk": ["external"]},
                "outcome": "deny",
            }
        ]
    )
    assert e.evaluate(call(risk="external"), PolicyContext(origin="daemon")).outcome == "deny"
    assert (
        e.evaluate(call(risk="external"), PolicyContext(origin="interactive")).outcome
        == "require_approval"
    )


def test_approved_call_still_obeys_hard_deny():
    e = engine(hard_deny=[{"id": "never", "match": {"tool": "fs_write"}}])
    decision = e.evaluate(
        call(name="fs_write", risk="write"), PolicyContext(autonomy="act", approved=True)
    )
    assert decision.outcome == "deny"


def test_approved_flag_clears_only_an_approval_requirement():
    e = engine()
    decision = e.evaluate(call(risk="write"), PolicyContext(autonomy="assist", approved=True))
    assert decision.outcome == "allow"


def test_subagent_autonomy_is_capped_by_its_caller():
    assert cap_autonomy("assist", "act") == "assist"
    assert cap_autonomy("act", "assist") == "assist"
    assert cap_autonomy("observe", "trusted") == "observe"


def test_unknown_risk_falls_back_to_the_configured_default():
    e = engine(defaults={"unknown_risk": "external"})
    decision = e.evaluate(call(risk="not-a-real-risk"), PolicyContext(autonomy="assist"))
    assert decision.outcome == "require_approval"


def test_rule_constraining_an_absent_argument_does_not_match():
    e = engine(rules=[{"id": "r", "match": {"args": {"network": {"equals": False}}}, "outcome": "allow"}])
    decision = e.evaluate(call(risk="write"), PolicyContext(autonomy="act"))
    assert decision.rule_id != "r"


def test_malformed_policy_fails_closed(tmp_path: Path):
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: 1\nrisk_matrix: {read: {assist: allow}}\nrules: [{id: x, match: {nope: 1}}]\n")
    with pytest.raises(PolicyError):
        load_policy(bad)


def test_unknown_placeholder_is_an_error(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\n"
        f"risk_matrix: {BASE_MATRIX}\n"
        "rules:\n"
        "  - id: r\n    match: {args: {path: {under: ['${nope}']}}}\n    outcome: allow\n"
    )
    with pytest.raises(PolicyError):
        load_policy(policy, {"workspace": ["/tmp"]})


def test_placeholders_expand_into_lists(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\n"
        f"risk_matrix: {BASE_MATRIX}\n"
        "rules:\n"
        "  - id: ws\n    match: {args: {path: {under: ['${workspace}']}}}\n    outcome: allow\n"
    )
    loaded = load_policy(policy, {"workspace": ["/tmp/ws", "/tmp/ws2"]})
    assert loaded.rules[0].match.args["path"].under == ["/tmp/ws", "/tmp/ws2"]


def test_shipped_policy_denies_reading_ssh_keys():
    from agentd.config import DEFAULT_POLICY
    from agentd.policy.engine import load_engine

    e = load_engine(
        DEFAULT_POLICY,
        {"allowed_roots": [str(Path.home() / "Projects")], "workspace": ["/tmp/ws"],
         "memory_repo": ["/tmp/mem"]},
    )
    decision = e.evaluate(
        ToolCallInfo(
            name="fs_read", risk="read", tags=("fs",),
            args={"path": str(Path.home() / ".ssh" / "id_ed25519")}, path_args=("path",),
        ),
        PolicyContext(autonomy="act"),
    )
    assert decision.outcome == "deny"


def _shipped():
    from agentd.config import DEFAULT_POLICY
    from agentd.policy.engine import load_engine

    return load_engine(
        DEFAULT_POLICY,
        {"allowed_roots": [str(Path.home() / "Projects")], "workspace": ["/tmp/ws"],
         "memory_repo": ["/tmp/mem"]},
    )


def test_a_connector_draft_in_the_background_asks_rather_than_dies():
    """daemon-never-external denies outright, and a denial in a background turn is silent.
    A connector's outbound work has to queue instead, or the central feature of having
    connectors at all — prepare it while you are away, you decide when you are back — is dead
    on arrival."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="mail_draft", risk="external", tags=("connector",)),
        PolicyContext(origin="daemon", autonomy="act"),
    )
    assert decision.outcome == "require_approval"
    assert decision.rule_id == "connector-drafts-queue-in-background"


def test_a_daemon_tool_without_the_connector_tag_is_still_denied():
    """The regression test that matters: the two rules inserted above daemon-never-external
    must not have punched a hole in it."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="web_post", risk="external"),
        PolicyContext(origin="daemon", autonomy="act"),
    )
    assert decision.outcome == "deny"
    assert decision.rule_id == "daemon-never-external"


def test_an_outbound_connector_asks_even_at_trusted_autonomy():
    decision = _shipped().evaluate(
        ToolCallInfo(name="mail_draft", risk="external", tags=("connector",)),
        PolicyContext(origin="interactive", autonomy="trusted"),
    )
    assert decision.outcome == "require_approval"
    assert decision.rule_id == "outbound-connector-always-asks"


@pytest.mark.parametrize("name", ["mail_send", "mail_forward"])
def test_there_is_no_tool_that_sends_mail(name):
    """Two halves. The policy half says a send tool would be refused even at trusted with an
    approval in hand; the registry half is the one that actually catches somebody adding it."""
    decision = _shipped().evaluate(
        ToolCallInfo(name=name, risk="external", tags=("connector",)),
        PolicyContext(autonomy="trusted", approved=True),
    )
    assert decision.outcome == "deny"

    from agentd.tools.registry import build_registry

    assert name not in build_registry().tools
