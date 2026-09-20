"""Telegram: a way for you to reach the loop from a phone, and for it to answer.

This is a **channel**, a third category beside connectors and tools, and getting that
straight is what keeps the rest of the architecture intact:

- A *connector* is inbound-only daemon-side code holding a credential the agent is fenced
  out of. Everything it ingests is archived untrusted, and it never sends anything.
- A *tool* is something the model calls, gated by the policy engine.
- A *channel* is neither. `agent chat` is one; so is this. It carries your words in and the
  loop's words out, and the model never calls it — which is why sending a reply here is not
  a policy decision, any more than printing to your terminal is.

The consequence worth stating plainly: the `no-mail-send-tool` tripwire and the private-data
interlock both remain exactly as strict. Nothing here gives the model a way to send a
message of its own choosing to anyone of its choosing. It answers you, on the thread you
started, and the only addresses it can reach are the ones in `allowed_chat_ids`.

**Long polling, not webhooks.** `getUpdates` with a long timeout needs no inbound port, no
public address and no certificate, which suits a headless box on a tailnet. A webhook would
need all three and would put an internet-facing listener on the same machine as the vault.

**The allowlist is the whole security boundary.** Anyone who learns a bot's username can
message it, and an unknown sender reaching an agent that holds your mail, your calendar and
your filesystem is the failure that matters. So an empty allowlist means nobody, a message
from an unlisted id is dropped and audited, and the first such message tells you once.

**What this costs, honestly.** Bot messages are not end-to-end encrypted; they cross
Telegram's servers in a form Telegram can read. Everything else here was built so nothing
leaves hardware you control, and this is the one place that stops being true. It is worth
knowing that your mail can now reach Telegram by way of an answer you asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID

import httpx

from .. import secrets as vault
from ..agent.events import TurnFinished, brief_args
from ..agent.loop import AgentLoop, Session
from ..config import Config
from ..db import repo_agenda, repo_ops
from ..policy.approvals import QueueApprover

HELP = (
    "I'm your agent.\n\n"
    "/new — start a fresh conversation (also how you lift the mail interlock)\n"
    "/status — what I'm watching and whether it's working\n"
    "/tools on|off — show which tools each answer used, and with what\n"
    "/approvals — anything waiting on your yes\n"
    "/approve <id> — say yes to one\n"
    "/whoami — your chat id"
)


def token() -> str | None:
    secret = vault.get("telegram/bot", "token")
    return secret.reveal() if secret else None


def configured(cfg: Config) -> str | None:
    """None when ready; otherwise the command that fixes it."""
    if token() is None:
        return "no bot token: run `agent secrets set telegram/bot token`"
    if not cfg.telegram.allowed_chat_ids:
        return (
            "no allowed_chat_ids: message the bot, run `agent telegram whoami` to see the id, "
            "then add it to [telegram] allowed_chat_ids"
        )
    return None


# --- the wire ----------------------------------------------------------------


async def call(
    cfg: Config, client: httpx.AsyncClient, method: str, **params: Any
) -> dict[str, Any]:
    """One Bot API call. Raises on transport failure; `supervise` restarts the loop."""
    bot = token()
    if bot is None:
        raise RuntimeError("no telegram bot token in the vault")
    response = await client.post(
        f"{cfg.telegram.api_base.rstrip('/')}/bot{bot}/{method}",
        json=params,
        # Long polling holds the connection open for poll_timeout_s; the read budget has to
        # be larger than that or every single poll ends in a timeout exception.
        timeout=cfg.telegram.poll_timeout_s + 15,
    )
    if response.status_code == 401:
        raise PermissionError("telegram rejected the bot token (401)")
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method}: {str(body.get('description'))[:200]}")
    return body.get("result") or {}


def chunk(text: str, limit: int) -> list[str]:
    """Telegram refuses long messages, so split rather than truncate.

    Prefers a paragraph break, then a line break, then a hard cut — an answer arriving in
    two readable halves beats one that stops mid-sentence.
    """
    text = text.strip() or "(no answer)"
    parts: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        # A paragraph break anywhere in the window beats a hard cut anywhere: a short first
        # chunk reads fine, a sentence severed mid-word does not. `> 0` rather than `>= 0`
        # keeps the loop advancing when the break is at position zero.
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return [p for p in parts if p]


async def say(cfg: Config, client: httpx.AsyncClient, chat_id: int, text: str) -> int | None:
    """Returns the id of the last message sent, so it can be edited in place."""
    sent = None
    for part in chunk(text, cfg.telegram.max_message_chars):
        result = await call(cfg, client, "sendMessage", chat_id=chat_id, text=part)
        sent = result.get("message_id", sent)
    return sent


SYMBOL = {"running": "\u2026", "ok": "\u2713", "error": "\u2717", "denied": "\u2717"}
NOTE = {"denied": " (refused by policy)", "error": " (failed)"}


def render_tools(lines: list[tuple[str, str, str]]) -> str:
    """One line per tool call, in the order they happened, with its arguments.

    The same thing `agent chat` prints, because the person reading a phone is the person
    reading the terminal and "which tools ran" without "on what" is not an account of what
    the agent did - it is a list of verbs. Both surfaces render through
    `events.brief_args`, so there is one rule about how much of somebody's words to show
    rather than one per surface.

    What that rule has to survive here: an argument can hold a subject line a stranger
    wrote, and this function joins calls with newlines. `brief_args` collapses whitespace
    for exactly that reason - without it a value containing a newline and a tick would
    forge a line claiming a tool ran that never did.

    Still not rendered: the tool's *result*. `ToolFinished.summary` is the output rather
    than the request, which for a mail search is a stranger's subject lines in full.
    """
    return "\n".join(
        f"{SYMBOL.get(state, '')} {name}({args}){NOTE.get(state, '')}".strip()
        for name, args, state in lines
    )


class Progress:
    """The status message that shows tools as they run, edited rather than re-sent.

    Re-sending would leave a trail of near-identical messages on a phone; editing keeps one
    line that fills in. Every Telegram call here is best-effort: a rate limit or a deleted
    message must not cost the answer the user actually asked for.
    """

    def __init__(self, cfg: Config, client: httpx.AsyncClient, chat_id: int, *, show: bool):
        self.cfg, self.client, self.chat_id, self.show = cfg, client, chat_id, show
        self.lines: list[tuple[str, str, str]] = []
        self.message_id: int | None = None

    async def started(self, name: str, args: dict | None = None) -> None:
        self.lines.append((name, brief_args(args or {}), "running"))
        await self._flush()

    async def finished(self, name: str, *, ok: bool, denied: bool) -> None:
        state = "denied" if denied else ("ok" if ok else "error")
        # Matched on the name alone: `ToolFinished` does not carry the arguments, and the
        # most recent running call of that name is the one that just ended.
        for index in range(len(self.lines) - 1, -1, -1):
            if self.lines[index][0] == name and self.lines[index][2] == "running":
                self.lines[index] = (name, self.lines[index][1], state)
                break
        else:
            self.lines.append((name, "", state))
        await self._flush()

    async def _flush(self) -> None:
        if not self.show or not self.lines:
            return
        text = render_tools(self.lines)
        with contextlib.suppress(Exception):
            if self.message_id is None:
                result = await call(
                    self.cfg, self.client, "sendMessage", chat_id=self.chat_id, text=text
                )
                self.message_id = result.get("message_id")
            else:
                await call(
                    self.cfg, self.client, "editMessageText",
                    chat_id=self.chat_id, message_id=self.message_id, text=text,
                )


# --- sessions ----------------------------------------------------------------


class Conversation:
    """One Telegram chat's session, resumed rather than rebuilt.

    Resuming matters more here than in the CLI: `Session.resume` restores `private`, so an
    interlock earned by reading mail on Monday is still standing on Tuesday rather than
    quietly lifting because the daemon restarted overnight.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sessions: dict[int, UUID] = {}
        self.verbose: dict[int, bool] = {}

    def show_tools(self, chat_id: int) -> bool:
        return self.verbose.get(chat_id, self.cfg.telegram.show_tools)

    async def session_for(self, chat_id: int, *, fresh: bool = False) -> Session:
        existing = self.sessions.get(chat_id)
        if existing is not None and not fresh:
            with contextlib.suppress(Exception):
                return await Session.resume(existing, autonomy=self.cfg.telegram.autonomy)
        session = await Session.create(channel="telegram", autonomy=self.cfg.telegram.autonomy)
        self.sessions[chat_id] = session.id
        return session


# --- commands ----------------------------------------------------------------


async def handle_command(
    cfg: Config, client: httpx.AsyncClient, conv: Conversation, chat_id: int, text: str
) -> bool:
    """Channel-level commands. Deliberately not tools: `/approve` here is you pressing the
    button, so it must not be something the model can reach for on its own."""
    command, _, rest = text.partition(" ")
    rest = rest.strip()

    if command in ("/start", "/help"):
        await say(cfg, client, chat_id, HELP)
    elif command == "/whoami":
        await say(cfg, client, chat_id, f"chat id {chat_id}")
    elif command == "/new":
        session = await conv.session_for(chat_id, fresh=True)
        await say(cfg, client, chat_id, f"new conversation ({str(session.id)[:8]}).")
    elif command == "/tools":
        want = rest.lower() not in ("off", "no", "false", "0")
        conv.verbose[chat_id] = want
        await say(cfg, client, chat_id, f"tool lines {'on' if want else 'off'}.")
    elif command == "/status":
        await say(cfg, client, chat_id, await status_line())
    elif command == "/approvals":
        rows = await repo_ops.list_approvals("pending", limit=10)
        if not rows:
            await say(cfg, client, chat_id, "Nothing waiting.")
        else:
            lines = [f"{r['id']} — {r['tool_name']} ({r['risk']})" for r in rows]
            await say(cfg, client, chat_id, "Waiting on you:\n" + "\n".join(lines))
    elif command == "/approve":
        await say(cfg, client, chat_id, await approve(rest))
    else:
        return False
    return True


async def status_line() -> str:
    from ..db import repo_connectors

    rows = await repo_connectors.list_state()
    if not rows:
        return "No connectors have run."
    return "\n".join(
        f"{r['name']}: {'ok' if r.get('last_success_at') else 'never run'}"
        + (f" · {r.get('items_seen', 0)} items" if r.get("items_seen") else "")
        + ("" if r.get("enabled", True) else " · DISABLED")
        for r in rows
    )


async def approve(approval_id: str) -> str:
    """Say yes from the phone, and mean it.

    Shares `execute_approved` with the CLI rather than reimplementing consent. Marking a row
    approved without running it would be worse than doing nothing: the CLI returns early on
    anything that is not `pending`, so a half-approval here would strand the action forever.
    """
    if not approval_id:
        return "Which one? /approvals lists them."
    try:
        target = UUID(approval_id)
    except ValueError:
        return "That is not an approval id."

    from ..policy.replay import execute_approved

    ok, message = await execute_approved(target, origin="telegram", note="approved by telegram")
    return message if ok else f"Not run: {message}"


# --- one message -------------------------------------------------------------


async def handle_message(
    cfg: Config, client: httpx.AsyncClient, conv: Conversation, message: dict
) -> None:
    chat_id = int((message.get("chat") or {}).get("id", 0))
    text = str(message.get("text") or "").strip()
    if not text:
        await say(cfg, client, chat_id, "I can only read text for now.")
        return

    if await handle_command(cfg, client, conv, chat_id, text):
        return

    session = await conv.session_for(chat_id)
    loop = AgentLoop(cfg=cfg, approver=QueueApprover(origin="telegram"))
    await call(cfg, client, "sendChatAction", chat_id=chat_id, action="typing")

    answer = ""
    denied: list[str] = []
    progress = Progress(cfg, client, chat_id, show=conv.show_tools(chat_id))

    async def run() -> None:
        nonlocal answer
        from ..agent.events import ToolFinished, ToolStarted

        async for event in loop.run_turn(
            session, text, origin="telegram", autonomy=cfg.telegram.autonomy
        ):
            if isinstance(event, ToolStarted):
                await progress.started(event.name, event.args)
            if isinstance(event, ToolFinished):
                await progress.finished(event.name, ok=event.ok, denied=event.denied)
                if event.denied:
                    denied.append(event.name)
            if isinstance(event, TurnFinished):
                answer = event.text

    task = asyncio.create_task(run())
    while not task.done():
        # A silent bot is indistinguishable from a broken one, and a local model on a busy
        # GPU is often neither.
        done, _ = await asyncio.wait({task}, timeout=cfg.telegram.slow_turn_s)
        if not done:
            await call(cfg, client, "sendChatAction", chat_id=chat_id, action="typing")
    await task

    if denied and not answer:
        answer = (
            "That needed something the policy refused: "
            + ", ".join(sorted(set(denied)))
            + ".\nIf this conversation has read your mail, /new lifts that."
        )
    await say(cfg, client, chat_id, answer)


async def refuse_stranger(chat_id: int, seen: set[int]) -> None:
    """An unlisted sender is dropped silently, and you are told once.

    Silently, because answering confirms the bot exists and is alive. Once, because a
    stranger who keeps trying should not be able to fill your phone with notifications.
    """
    await repo_ops.write_action(
        repo_ops.ActionRecord(
            actor="channel:telegram", kind="telegram_message", name="rejected",
            status="denied", output={"chat_id": chat_id},
        )
    )
    if chat_id in seen:
        return
    seen.add(chat_id)
    await repo_agenda.notify(
        source="channel:telegram",
        level="warn",
        title="Someone else messaged your agent's bot",
        body=(
            f"chat id {chat_id} is not in [telegram] allowed_chat_ids, so it was ignored. "
            "Add it only if that is you."
        ),
    )


# --- the supervised loop -----------------------------------------------------


async def telegram_loop(cfg: Config, stop: asyncio.Event) -> None:
    """Registered in `daemon/main.py` beside the notifier and the connectors."""
    if not cfg.telegram.enabled:
        await stop.wait()
        return
    why = configured(cfg)
    if why:
        # Park like a connector does: a channel pointed at nothing should explain itself
        # rather than crash-loop.
        await repo_agenda.notify(
            source="channel:telegram", level="warn",
            title="Telegram is enabled but not set up", body=why,
        )
        await stop.wait()
        return

    conv = Conversation(cfg)
    allowed = set(cfg.telegram.allowed_chat_ids)
    strangers: set[int] = set()
    offset = 0

    async with httpx.AsyncClient() as client:
        # Drop anything queued while the daemon was down. Answering a question from six
        # hours ago is worse than not answering it.
        with contextlib.suppress(Exception):
            for update in await call(cfg, client, "getUpdates", offset=-1, timeout=0):
                offset = max(offset, int(update.get("update_id", 0)) + 1)

        while not stop.is_set():
            try:
                updates = await call(
                    cfg, client, "getUpdates",
                    offset=offset, timeout=cfg.telegram.poll_timeout_s,
                    allowed_updates=["message"],
                )
            except PermissionError:
                await repo_agenda.notify(
                    source="channel:telegram", level="error",
                    title="Telegram rejected the bot token",
                    body="Fix it with `agent secrets set telegram/bot token`, then restart.",
                )
                await stop.wait()
                return
            except Exception:
                # The internet being the internet. Back off a little and keep going; the
                # offset is unchanged, so nothing is lost.
                await asyncio.wait([asyncio.create_task(stop.wait())], timeout=5)
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                chat_id = int((message.get("chat") or {}).get("id", 0))
                if chat_id not in allowed:
                    await refuse_stranger(chat_id, strangers)
                    continue
                try:
                    await handle_message(cfg, client, conv, message)
                except Exception as exc:
                    with contextlib.suppress(Exception):
                        await say(cfg, client, chat_id, f"That went wrong: {type(exc).__name__}")
                    await repo_ops.write_action(
                        repo_ops.ActionRecord(
                            actor="channel:telegram", kind="telegram_message", name="turn",
                            status="error", error=str(exc)[:300],
                        )
                    )
