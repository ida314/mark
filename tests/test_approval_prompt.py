"""An approval prompt has to say who is asking.

Since a delegated worker inherits the caller's approver, a sub-agent can interrupt an
`agent chat` session mid-delegation and ask for `fs_write` in exactly the terminal the
orchestrator uses. Dylan accepted being prompted mid-delegation; he did not accept being
prompted anonymously. `origin` is the only attribution the request carries - a worker's is
`subagent:<role>`, the orchestrator's is `interactive` - so if the panel drops it, the
person answering a stream of prompts during an attended run has no way to tell whose
write they are authorising.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from agentd.cli import chat
from agentd.policy.approvals import ApprovalRequest, SessionGrants
from agentd.policy.engine import Decision


class _StubPrompt:
    """Answers 'no', then gives no reason, so `request` returns without blocking."""

    def __init__(self) -> None:
        self.answers = ["n", ""]

    async def prompt_async(self, *_args: object, **_kwargs: object) -> str:
        return self.answers.pop(0)


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch):
    async def render(origin: str) -> str:
        buffer = io.StringIO()
        monkeypatch.setattr(
            chat, "console", Console(file=buffer, width=200, no_color=True, highlight=False)
        )
        approver = chat.CliApprover(SessionGrants(), _StubPrompt())
        result = await approver.request(
            ApprovalRequest(
                tool_name="fs_write",
                args={"path": "/home/dylan/Projects/agent/src/agentd/cli/chat.py"},
                risk="high",
                decision=Decision(outcome="ask", rule_id="fs_write.default"),
                origin=origin,
            )
        )
        assert result.approved is False
        return buffer.getvalue()

    return render


async def test_an_approval_asked_for_by_a_delegated_worker_names_that_worker(rendered) -> None:
    panel = await rendered("subagent:coder")

    assert "coder" in panel
    assert "fs_write" in panel


async def test_the_orchestrators_own_approval_does_not_claim_a_worker(rendered) -> None:
    panel = await rendered("interactive")

    assert "worker" not in panel.lower()
    assert "subagent" not in panel.lower()
    assert "fs_write" in panel


async def test_a_worker_prompt_and_an_orchestrator_prompt_do_not_look_alike(rendered) -> None:
    """Told apart at a glance, not by reading: the two panels differ in their framing."""
    worker_panel = await rendered("subagent:coder")
    orchestrator_panel = await rendered("interactive")

    worker_first_line = worker_panel.splitlines()[0]
    orchestrator_first_line = orchestrator_panel.splitlines()[0]
    assert worker_first_line != orchestrator_first_line
    assert "coder" in worker_first_line
