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
