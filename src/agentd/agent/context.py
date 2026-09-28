"""Assembling what the model sees: instructions, retrieved memory, history window."""

from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Any
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


# How much of one replayed tool result the next turn is shown. The model needs to know what
# it called and what came back, not the whole of a 20KB file listing: the full result is in
# the archive, the manifest can cite it, and a history that carried results in full would
# spend `agent.history_tokens` on three turns.
HISTORY_TOOL_RESULT_CHARS = 600


def replay_tool_calls(rows: list[dict], *, result_chars: int = HISTORY_TOOL_RESULT_CHARS) -> list[dict]:
    """One turn's archived tool calls as the messages the model saw when it made them.

    An `assistant` message carrying every call of the turn, then one `tool` message per
    result, in the order they ran - the same shape `loop.run_turn` appends live, so a
    prompt rebuilt from the archive has the same structure as the prompt that produced it.
    All of a turn's calls go on one assistant message rather than one per step: the archive
    does not record which step a call belonged to, and one message with N calls is a valid
    shape while a `tool` message with no call before it is not.

    A row archived before `call_id` was recorded gets one minted from its archive id, which
    is stable and unique, and that is all a tool_call id has to be.
    """
    if not rows:
        return []
    calls: list[dict] = []
    results: list[dict] = []
    for row in rows:
        payload = row.get("payload") or {}
        call_id = str(payload.get("call_id") or f"h{row['id']}")
        args = payload.get("args")
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": row["actor"].removeprefix("tool:"),
                    "arguments": json.dumps(args if isinstance(args, dict) else {}),
                },
            }
        )
        content = row.get("content") or ""
        if len(content) > result_chars:
            content = (
                content[:result_chars]
                + f"\n… [truncated by the runtime; {len(content)} chars in full]"
            )
        results.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return [{"role": "assistant", "content": None, "tool_calls": calls}, *results]


def _unit_cost(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        for call in m.get("tool_calls") or ():
            total += estimate_tokens(call["function"]["arguments"])
    return total


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

    Tool calls are replayed with the prose (2026-09-28). Before this, a turn's calls and
    their results were archived and never shown again, so on the next message the model
    was reading a reply of its own that referred to work it could no longer see - and a
    27B asked "why did that fail?" answered that it had never called anything. The calls
    of a turn go immediately before that turn's answer, as one unit: a budget cut that
    kept the answer and dropped the calls would recreate the exact gap this closes.
    """
    rows = await repo_archive.recent_messages(session_id, limit=200, after_id=after_id)
    turn_ids = sorted({row["turn_id"] for row in rows if row.get("turn_id")}, key=str)
    tool_rows = await repo_archive.recent_tool_results(session_id, turn_ids, after_id=after_id)
    by_turn: dict[Any, list[dict]] = {}
    for row in tool_rows:
        by_turn.setdefault(row["turn_id"], []).append(row)

    # Oldest first, in units the budget cut cannot split. A turn's calls and its answer are
    # one unit, so the cut keeps both or neither - an answer kept without the calls behind
    # it is the exact gap this replay closes. A turn that has calls but no answer row (it
    # died, or answered nothing) gets them after its user message instead, so they are not
    # lost and never precede the message that caused them.
    units: list[list[dict]] = []
    answered: set[Any] = {row["turn_id"] for row in rows if row["kind"] == "assistant_message"}
    for row in rows:
        content = row.get("content") or ""
        turn = row.get("turn_id")
        if row["kind"] == "user_message":
            units.append([{"role": "user", "content": content}])
            if turn not in answered and turn in by_turn:
                units.append(replay_tool_calls(by_turn.pop(turn)))
        else:
            calls = replay_tool_calls(by_turn.pop(turn)) if turn in by_turn else []
            units.append([*calls, {"role": "assistant", "content": content}])

    picked: list[dict] = []
    used = 0
    for unit in reversed(units):
        cost = _unit_cost(unit)
        if used + cost > budget_tokens:
            break
        used += cost
        picked.extend(reversed(unit))
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
    extra_system: str = "",
) -> list[dict]:
    """The prompt for one turn.

    `handoff_block` is how a fresh orchestrator is started after a context handoff: it goes
    into the same system message rather than a second one, so the shape of the message list
    is what it always was - system, conversation, the user - and the only thing that changed
    is that the conversation now begins after the handoff's watermark.

    `extra_system` is a delegated worker's role prompt and is folded in the same way, for a
    harder reason than tidiness: Qwen3's chat template rejects any system message that is
    not the single leading one with `HTTP 400 System message must be at the beginning`, so
    a second system message is not a longer prompt, it is a turn that never reaches the
    model at all.
    """
    cfg = cfg or get_config()
    system = system_prompt(cfg, autonomy, context_block)
    if handoff_block.strip():
        system = f"{system}\n{handoff_block}"
    if extra_system.strip():
        system = f"{system}\n{extra_system}"
    return [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_text},
    ]
