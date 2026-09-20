"""Executing a queued approval, in one place.

There are two surfaces that can say yes — the CLI and the Telegram channel — and for a
while there were nearly two answers to what yes *means*. That is the kind of divergence
that ends with an approval marked `approved` by one surface and never executed by the
other, because the second one checks `status != "pending"` and returns.

So: deciding and executing are one operation, and it lives here.

Three properties this preserves, all of which predate it:

- **The argument hash is re-checked.** An approval row records the arguments it was asked
  about, and consent is to *those*, not to whatever the row says by the time somebody gets
  round to reading it.
- **The call goes back through the full executor**, so `hard_deny` is evaluated again. An
  approval in hand has never been able to buy a hard-denied call, and `tests/test_policy.py`
  pins that.
- **The replay context is fresh.** It does not inherit the session flags of the turn that
  queued the call — including `private`. That is exactly why the private-data interlock
  denies rather than queues: a queued call would be executed later by this function, in a
  context that has forgotten the mailbox was ever read.
"""

from __future__ import annotations

from uuid import UUID

from ..db import repo_ops


async def execute_approved(
    approval_id: UUID, *, origin: str = "interactive", note: str | None = None
) -> tuple[bool, str]:
    """Approve and run one queued call. Returns (ok, a sentence for whoever asked)."""
    from ..config import get_config
    from ..policy.approvals import AutoApprover
    from ..policy.engine import engine_from_config
    from ..tools.base import ToolContext
    from ..tools.executor import ToolExecutor
    from ..tools.registry import get_registry

    row = await repo_ops.get_approval(approval_id)
    if row is None:
        return False, "No such approval."
    if row["status"] != "pending":
        return False, f"Already {row['status']}."

    args = row["args"]
    if repo_ops.args_hash(args) != row["args_sha256"]:
        return False, "The arguments changed since it was queued; refusing."

    await repo_ops.decide_approval(approval_id, "approved", "user", note)
    cfg = get_config()
    executor = ToolExecutor(get_registry().tools, engine_from_config(cfg), AutoApprover(True))
    result = await executor.run(
        row["tool_name"],
        args,
        ToolContext(
            session_id=row.get("session_id"), turn_id=row.get("turn_id"), actor="user",
            origin=origin, autonomy="act",
        ),
    )
    await repo_ops.finish_approval(
        approval_id,
        "executed" if result.ok else "failed",
        {"content": result.content[:2000], "ok": result.ok},
    )
    return result.ok, result.content[:2000]


async def deny_approval(approval_id: UUID, note: str | None = None) -> tuple[bool, str]:
    row = await repo_ops.get_approval(approval_id)
    if row is None:
        return False, "No such approval."
    if row["status"] != "pending":
        return False, f"Already {row['status']}."
    await repo_ops.decide_approval(approval_id, "denied", "user", note)
    return True, f"Denied {row['tool_name']}."
