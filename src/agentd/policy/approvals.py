"""Approval flows: ask the human in front of us, or queue for later."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from ..db import repo_ops
from .engine import Decision


@dataclass
class ApprovalRequest:
    tool_name: str
    args: dict[str, Any]
    risk: str
    decision: Decision
    reason: str | None = None  # the agent's own justification
    preview: str | None = None
    origin: str = "interactive"
    session_id: UUID | None = None
    turn_id: UUID | None = None


@dataclass
class ApprovalResult:
    approved: bool
    note: str | None = None
    queued_id: UUID | None = None
    decided_by: str = "user"


class Approver(Protocol):
    async def request(self, req: ApprovalRequest) -> ApprovalResult: ...


@dataclass
class SessionGrants:
    """'Allow for this session' memory. Never persisted to disk."""

    exact: set[tuple[str, str]] = field(default_factory=set)
    dirs: set[tuple[str, str]] = field(default_factory=set)

    def grant(self, tool: str, args: dict[str, Any]) -> None:
        self.exact.add((tool, repo_ops.args_hash(args)))
        path = args.get("path")
        if isinstance(path, str):
            from pathlib import Path

            self.dirs.add((tool, str(Path(path).parent)))

    def granted(self, tool: str, args: dict[str, Any]) -> bool:
        if (tool, repo_ops.args_hash(args)) in self.exact:
            return True
        path = args.get("path")
        if isinstance(path, str):
            from pathlib import Path

            return (tool, str(Path(path).parent)) in self.dirs
        return False


@dataclass
class RerunGrants:
    """'The user said to run that one again', for a call a crash left uncertain.

    Session 5c, and Dylan's requirement B at the Pass 4/5 boundary. After a crash an
    `unsafe_write` that was announced and never reported back is closed as *uncertain*: it
    may have happened. `agent/observations.py` tells the model so and tells it not to re-run
    the call on its own - and until now that sentence was the entire guard. A re-issued call
    mints a *new* idempotency key, because `step_id` is part of the key and the resumed turn
    is at a different step, so nothing downstream recognises it as the same call. The model
    being asked to comply is a local 27B.

    So the refusal moved into the executor, and this is the only way past it. Same shape as
    `SessionGrants` and for the same reason: never persisted, never inferred, and never
    granted by the runtime on the model's behalf. A grant is one (run, tool, arguments) that
    a person named.

    Keyed on the run because that is the scope of the doubt. The uncertain call belongs to
    one run; a later run doing the same thing is ordinary work, not a duplicate.
    """

    exact: set[tuple[str, str, str]] = field(default_factory=set)

    def allow(self, *, run_id: str, tool: str, args_hash: str) -> None:
        self.exact.add((run_id, tool, args_hash))

    def allow_tool(self, *, run_id: str, tool: str) -> None:
        """Every uncertain call of one tool in one run.

        The coarse grain exists because the fine one is unusable at a terminal: the thing a
        person can see and act on is "re-send the two emails that may not have gone", not a
        64-character digest. `ANY` is a sentinel rather than an empty string, which would be
        a real hash that never matches and would look identical in a set dump.
        """
        self.exact.add((run_id, tool, ANY))

    def granted(self, *, run_id: str, tool: str, args_hash: str) -> bool:
        return (
            (run_id, tool, args_hash) in self.exact
            or (run_id, tool, ANY) in self.exact
        )


# Not "" and not None: both would be values a real digest could be confused with by a
# reader, and this one is unmistakable in a dump of the set.
ANY = "*any-arguments*"


class AutoApprover:
    """Approves or denies everything. Tests and non-interactive scripts only."""

    def __init__(self, approve: bool = True) -> None:
        self.approve = approve
        self.seen: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        self.seen.append(req)
        return ApprovalResult(approved=self.approve, note=None, decided_by="auto")


class QueueApprover:
    """No human is present: persist the request and let the model move on."""

    def __init__(self, origin: str = "daemon") -> None:
        self.origin = origin

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        approval_id = await repo_ops.queue_approval(
            tool_name=req.tool_name,
            args=req.args,
            risk=req.risk,
            policy_rule=req.decision.rule_id,
            origin=req.origin or self.origin,
            reason=req.reason,
            preview=req.preview,
            session_id=req.session_id,
            turn_id=req.turn_id,
        )
        return ApprovalResult(approved=False, queued_id=approval_id, decided_by="queued")
