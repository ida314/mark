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


@pytest.mark.parametrize(
    "name",
    ["mail_send", "mail_forward", "gmail_send", "gmail_forward", "gmail_modify"],
)
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


# --- the private-data interlock ----------------------------------------------
#
# Reading the user's mailbox raises `private` for the session. These assert the door it
# closes, and — just as importantly — that it stays open when it has not been raised.


PRIVATE_DENIED = [
    ("web_fetch", "read", ("web", "untrusted", "egress"), {}, "builtin", "private-data-no-egress"),
    ("web_search", "read", ("web", "untrusted", "egress"), {}, "builtin", "private-data-no-egress"),
    ("shell_exec", "write", ("sandbox", "shell", "egress"), {"network": True}, "builtin",
     "private-data-no-egress"),
    ("delegate", "read", ("core",), {"agent": "researcher"}, "builtin",
     "private-data-no-outward-delegation"),
    ("delegate", "read", ("core",), {"agent": "coder"}, "builtin",
     "private-data-no-outward-delegation"),
    ("fs_write", "write", ("fs",), {}, "builtin", "private-data-no-writes"),
    ("watcher_add", "write", (), {}, "builtin", "private-data-no-writes"),
    ("notes_search", "read", ("mcp",), {}, "mcp:notes", "private-data-no-mcp"),
    ("some_future_sender", "external", (), {}, "builtin", "private-data-no-writes"),
]


@pytest.mark.parametrize("name,risk,tags,args,source,rule", PRIVATE_DENIED)
def test_a_session_that_read_your_mail_cannot_reach_out(name, risk, tags, args, source, rule):
    decision = _shipped().evaluate(
        ToolCallInfo(name=name, risk=risk, tags=tags, args=args, source=source),
        PolicyContext(autonomy="assist", private=True),
    )
    assert decision.outcome == "deny"
    assert decision.rule_id == rule


@pytest.mark.parametrize("name,risk,tags,args,source,rule", PRIVATE_DENIED)
def test_none_of_that_changes_until_the_mailbox_is_actually_read(
    name, risk, tags, args, source, rule
):
    """The regression that matters most. An interlock that fires when it should not is an
    interlock somebody turns off."""
    decision = _shipped().evaluate(
        ToolCallInfo(name=name, risk=risk, tags=tags, args=args, source=source),
        PolicyContext(autonomy="assist", private=False),
    )
    assert decision.outcome != "deny" or decision.rule_id != rule


def test_the_interlock_is_not_the_taint_rule():
    """Taint is raised by every web page and by shell output. Hard-denying on it would mean
    fetching page 1 of a search makes page 2 impossible, which is why `private` is its own
    flag rather than a severity level on this one."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="web_fetch", risk="read", tags=("web", "untrusted", "egress")),
        PolicyContext(autonomy="assist", tainted=True),
    )
    assert decision.outcome == "allow"


def test_an_airgapped_sandbox_still_runs_after_reading_mail():
    """`network` absent means no network, and engine.py's absent-argument rule is what makes
    the distinction load-bearing rather than decorative."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="shell_exec", risk="write", tags=("sandbox", "shell"),
                     args={"network": False}),
        PolicyContext(autonomy="assist", private=True),
    )
    assert decision.rule_id != "private-data-no-egress"


def test_local_delegation_survives_the_interlock():
    """delegate(memory) never builds a sub-agent; it packs retrieval in this process."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="delegate", risk="read", tags=("core",), args={"agent": "memory"}),
        PolicyContext(autonomy="assist", private=True),
    )
    assert decision.outcome == "allow"


def test_the_mail_tools_do_not_lock_themselves_out():
    """Reading a second message must not be denied by the interlock the first one raised."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="gmail_message", risk="read", tags=("mail", "untrusted")),
        PolicyContext(autonomy="assist", private=True),
    )
    assert decision.outcome == "allow"


def test_telling_the_user_still_works_after_reading_their_mail():
    """An interlock that also gags the agent would just make it fail silently."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="notify_user", risk="draft", tags=("core",)),
        PolicyContext(autonomy="assist", private=True),
    )
    assert decision.outcome == "allow"


def test_the_interlock_survives_an_approval_in_hand():
    """`agent approvals approve` replays a queued call in a *fresh* context that has
    forgotten the mailbox was ever read. That is precisely why these are hard denies rather
    than require_approval: a queued call is a delayed allow."""
    decision = _shipped().evaluate(
        ToolCallInfo(name="web_fetch", risk="read", tags=("web", "untrusted", "egress")),
        PolicyContext(autonomy="trusted", private=True, approved=True),
    )
    assert decision.outcome == "deny"


def test_the_mailbox_is_never_read_unattended():
    for origin, expected in (("daemon", "deny"), ("interactive", "allow")):
        decision = _shipped().evaluate(
            ToolCallInfo(name="gmail_search", risk="read", tags=("mail", "untrusted")),
            PolicyContext(autonomy="observe" if origin == "daemon" else "assist", origin=origin),
        )
        assert decision.outcome == expected, origin


# Tools that may still run once the mailbox has been opened. Everything else must be denied,
# so adding a tool forces a deliberate decision here rather than silently widening the door.
PRIVATE_SAFE = {
    # A local SELECT over rows the daemon already archived. The interlock exists to shut
    # the *egress* door once untrusted mail is in context, and this sends nothing anywhere.
    # Denying it would break the one workflow the pairing is for: read the invitation, then
    # look at what it collides with.
    # Same argument for coursework: a SELECT over archived deadlines, no egress, and
    # "what is due before that trip" is the same read-then-compare workflow.
    "calendar_upcoming", "coursework_due",
    # The manifest lookup (session 5d). It resolves one ref against *this session's own*
    # archive and sends nothing anywhere, so the egress door it would open is none. The
    # reason it is safe under the interlock rather than merely harmless is stricter than
    # that and lives in the tool: a manifest item marked private is refused outright, so
    # the one thing the interlock is protecting cannot come back through this door even
    # though the door itself is local.
    "handoff_lookup",
    "fs_list", "fs_read", "fs_search", "gmail_message", "gmail_search", "goal_upsert",
    "goals_list", "memory_history", "memory_remember", "memory_search", "notify_user",
    "open_loop_add", "open_loop_close", "open_loops_list", "profile_read", "reminder_set",
    "time_now", "tool_search",
    # Session 7b's scratchpad. The interlock shuts the *egress* door once the user's private
    # data is in context, and these two open none: a note is journaled under this one run and
    # this one agent's scope, is readable only by the agent that wrote it, and is discarded
    # when the task ends. Denying them would break the workflow the interlock exists to
    # preserve - read the mail, then reason about it - by taking away the place the reasoning
    # is kept. The note carries `private=True` when it was written under the interlock, which
    # is what a later promotion pass has to check before any of it becomes durable.
    "working_memory_note", "working_memory_list",
    # `delegate` is here only because this probe passes no arguments, and the rules that
    # stop it are keyed on `agent`, which the schema makes required. The real denials are
    # asserted by name above; nothing can call delegate without saying which sub-agent.
    "delegate",
}


def test_every_new_tool_must_decide_where_it_stands():
    """Inverted default. A tool added next year is denied under `private` unless somebody
    puts it on this list on purpose — the same philosophy as the send-tool tripwires."""
    from agentd.tools.registry import build_registry

    engine = _shipped()
    for tool in build_registry().enabled():
        decision = engine.evaluate(
            ToolCallInfo(name=tool.name, risk=tool.risk, tags=tool.tags, source=tool.source),
            PolicyContext(autonomy="assist", private=True),
        )
        if tool.name in PRIVATE_SAFE:
            continue
        assert decision.outcome == "deny", f"{tool.name} is neither denied nor on PRIVATE_SAFE"
