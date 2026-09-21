"""Assembling what the model sees: instructions, retrieved memory, history window."""

from __future__ import annotations

import getpass
from pathlib import Path
from uuid import UUID

from ..config import Config, get_config
from ..db import repo_archive
from ..ids import estimate_tokens, utcnow

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text()


def user_instructions(cfg: Config) -> str:
    """The user's own standing instructions, owned by them, never written by the agent."""
    path = cfg.paths.memory_repo / "agent" / "instructions.md"
    if path.is_file():
        text = path.read_text().strip()
        if text:
            return f"\n## Standing instructions from the user\n\n{text}\n"
    return ""


def system_prompt(cfg: Config, autonomy: str, context_block: str = "") -> str:
    base = load_prompt("main.md").format(
        user_name=getpass.getuser(),
        now=utcnow().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        autonomy=autonomy,
    )
    parts = [base, user_instructions(cfg)]
    if context_block.strip():
        parts.append(
            "\n## What you know right now\n\n"
            "(Retrieved from memory for this turn. Each line names its source, how old it "
            "is, and whether it has been adjudicated. `[F:id]` are adjudicated facts; "
            "`[C:id]` are provisional claims still in review, which may say `disputed by` "
            "another handle when something else contends the same point.)\n\n" + context_block
        )
    return "\n".join(p for p in parts if p)


async def history_messages(
    session_id: UUID,
    *,
    budget_tokens: int,
    summary: str | None = None,
    after_id: int | None = None,
) -> list[dict]:
    """Recent turns, newest-first until the budget is spent, then re-ordered.

    `after_id` is a handoff watermark (session 5b). When a conversation has handed off,
    everything at or below it is already stated in the handoff object the system block
    carries, so replaying it would be the old context copied in beside its own summary.
    """
    rows = await repo_archive.recent_messages(session_id, limit=200, after_id=after_id)
    picked: list[dict] = []
    used = 0
    for row in reversed(rows):
        content = row.get("content") or ""
        cost = estimate_tokens(content)
        if used + cost > budget_tokens:
            break
        used += cost
        picked.append(
            {
                "role": "user" if row["kind"] == "user_message" else "assistant",
                "content": content,
            }
        )
    picked.reverse()
    if summary and len(picked) < len(rows):
        picked.insert(
            0,
            {
                "role": "system",
                "content": f"Summary of earlier conversation in this session:\n{summary}",
            },
        )
    return picked


def build_messages(
    cfg: Config,
    *,
    autonomy: str,
    context_block: str,
    history: list[dict],
    user_text: str,
    handoff_block: str = "",
) -> list[dict]:
    """The prompt for one turn.

    `handoff_block` is how a fresh orchestrator is started after a context handoff: it goes
    into the same system message rather than a second one, so the shape of the message list
    is what it always was - system, conversation, the user - and the only thing that changed
    is that the conversation now begins after the handoff's watermark.
    """
    cfg = cfg or get_config()
    system = system_prompt(cfg, autonomy, context_block)
    if handoff_block.strip():
        system = f"{system}\n{handoff_block}"
    return [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_text},
    ]
