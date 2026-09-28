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
from ..agent.loop import AgentLoop, Session
from ..agent.stream import Answer
from ..config import Config
from ..db import repo_agenda, repo_ops
from ..ids import uuid7
from ..journal.feed import JournalTail, turn_ended
from ..journal.render import brief_args
from ..journal.store import Event
from ..policy.approvals import ApprovalRequest, ApprovalResult

HELP = (
    "I'm your agent.\n\n"
    "/new — start a fresh conversation (also how you lift the mail interlock)\n"
    "/status — what I'm watching and whether it's working\n"
    "/tools on|off — show which tools each answer used, and with what\n"
    "/approvals — anything waiting on your yes\n"
    "/approve <id> — say yes to one you did not tap in time\n"
    "/whoami — your chat id\n\n"
    "When something needs your say-so I'll ask here with Approve / Deny buttons and wait."
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

    Still not rendered: the tool's *result*. `tool_finished.summary` in the journal is the
    output rather than the request, which for a mail search is a stranger's subject lines in
    full.
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
        # Matched on the name alone: the terminal journal event carries the `call_id` but
        # not the arguments, and the most recent running call of that name is the one that
        # just ended.
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


# --- approvals from the phone ------------------------------------------------

APPROVE, DENY = "approve", "deny"


def approval_text(req: ApprovalRequest, approval_id: UUID) -> str:
    """What the phone shows above the two buttons. Plain text, no parse mode: a preview is a
    diff or a shell command, and either would need escaping under Markdown that a missed
    character turns into a 400 - and a refused approval message is a turn that waits for a
    tap that can never come."""
    worker = req.origin.removeprefix("subagent:") if req.origin.startswith("subagent:") else None
    lines = [
        f"Approval needed: {req.tool_name} ({req.risk})",
        f"requested by the {worker} worker" if worker else "requested by the agent",
    ]
    if req.reason:
        lines.append(f"why: {req.reason}")
    if req.preview:
        preview = req.preview.strip()
        if len(preview) > 1500:
            preview = preview[:1500] + "\n…"
        lines.append("")
        lines.append(preview)
    lines.append("")
    lines.append(f"id {approval_id}")
    return "\n".join(lines)


def approval_keyboard(approval_id: UUID) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"{APPROVE}:{approval_id}"},
                {"text": "Deny", "callback_data": f"{DENY}:{approval_id}"},
            ]
        ]
    }


def parse_tap(data: str) -> tuple[bool, UUID] | None:
    """`(approved, id)` from a button's callback data, or None for anything else."""
    verb, _, raw = (data or "").partition(":")
    if verb not in (APPROVE, DENY):
        return None
    try:
        return verb == APPROVE, UUID(raw)
    except ValueError:
        return None


class Taps:
    """The approvals some running turn is waiting on, keyed by approval id.

    A tap arrives on the poll loop and the turn that asked is a task somewhere else; a
    future per request is the whole handshake. Nothing here is durable, on purpose: the
    request was queued in Postgres *before* the buttons were sent, so a daemon that dies
    while waiting loses the wait and not the request - the button still works afterwards,
    it just runs through `execute_approved` instead of resolving a future.
    """

    def __init__(self) -> None:
        self.pending: dict[UUID, asyncio.Future[bool]] = {}

    def expect(self, approval_id: UUID) -> asyncio.Future[bool]:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = fut
        return fut

    def forget(self, approval_id: UUID) -> None:
        self.pending.pop(approval_id, None)

    def resolve(self, approval_id: UUID, approved: bool) -> bool:
        """Hand a decision to the waiting turn. False when nothing is waiting for it."""
        fut = self.pending.pop(approval_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True


class TelegramApprover:
    """Ask the person holding the phone, and wait for the tap.

    The approver a Telegram turn is built with, in place of the `QueueApprover` it used to
    get. That one told the model "nobody is at the keyboard" to a user who was reading the
    reply on their phone, and parked the write under an id the user was never shown
    (session 01a0e9f6). The person is present; they just have no keyboard.

    Order matters: the row is queued first, then the buttons are sent, then the wait. So at
    every point the request is at least as durable as it was under `QueueApprover`, and the
    tap is an improvement on the queue rather than a replacement for it. A tap that comes
    in time resolves the row and the turn carries on with the real result. No tap within
    `approval_timeout_s` leaves the row `pending` - `/approve <id>` runs it later, exactly
    as before - and the model is told what actually happened.

    A worker asks through this too: `run_turn` hands the loop's approver down through
    `ctx.extra["approver"]`, so a coder's `fs_write` reaches the phone naming the worker.
    """

    def __init__(self, cfg: Config, client: httpx.AsyncClient, chat_id: int, taps: Taps) -> None:
        self.cfg, self.client, self.chat_id, self.taps = cfg, client, chat_id, taps

    async def request(self, req: ApprovalRequest) -> ApprovalResult:
        approval_id = await repo_ops.queue_approval(
            tool_name=req.tool_name, args=req.args, risk=req.risk,
            policy_rule=req.decision.rule_id, origin=req.origin or "telegram",
            reason=req.reason, preview=req.preview,
            session_id=req.session_id, turn_id=req.turn_id,
        )
        fut = self.taps.expect(approval_id)
        try:
            sent = await call(
                self.cfg, self.client, "sendMessage",
                chat_id=self.chat_id, text=approval_text(req, approval_id),
                reply_markup=approval_keyboard(approval_id),
            )
        except Exception as exc:
            # Could not ask. The row is queued, which is what would have happened anyway;
            # say so rather than claim a wait that never started.
            self.taps.forget(approval_id)
            return ApprovalResult(
                approved=False, queued_id=approval_id, decided_by="queued",
                note=(
                    f"Could not reach you on Telegram ({type(exc).__name__}). It is queued as "
                    f"{approval_id}; /approve {approval_id} runs it."
                ),
            )
        message_id = sent.get("message_id")
        timeout = self.cfg.telegram.approval_timeout_s
        try:
            approved = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self.taps.forget(approval_id)
            await self._edit(
                message_id,
                f"No answer in {int(timeout)}s — still queued as {approval_id}. "
                f"/approve {approval_id} runs it.",
            )
            return ApprovalResult(
                approved=False, queued_id=approval_id, decided_by="queued",
                note=(
                    f"The user was asked on Telegram and did not answer within {int(timeout)} "
                    f"seconds. It stays queued as {approval_id}; tell them that "
                    f"/approve {approval_id} runs it. Continue with something else."
                ),
            )
        await repo_ops.decide_approval(
            approval_id, "approved" if approved else "denied", "user", "telegram tap"
        )
        return ApprovalResult(
            approved=approved, decided_by="user",
            note=None if approved else "The user denied this on Telegram.",
        )

    async def _edit(self, message_id: int | None, text: str) -> None:
        if message_id is None:
            return
        with contextlib.suppress(Exception):
            await call(
                self.cfg, self.client, "editMessageText",
                chat_id=self.chat_id, message_id=message_id, text=text,
            )


async def handle_tap(
    cfg: Config, client: httpx.AsyncClient, conv: Conversation, query: dict
) -> None:
    """A button press. Resolves the waiting turn when there is one, and otherwise - the
    daemon restarted, or the wait timed out - acts on the queued row directly, so the
    buttons never go dead."""
    query_id = str(query.get("id") or "")
    message = query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id", 0))
    message_id = message.get("message_id")
    parsed = parse_tap(str(query.get("data") or ""))

    async def ack(text: str) -> None:
        with contextlib.suppress(Exception):
            await call(cfg, client, "answerCallbackQuery", callback_query_id=query_id, text=text)

    if parsed is None:
        await ack("That button is not one of mine.")
        return
    approved, approval_id = parsed
    verdict = "Approved" if approved else "Denied"
    if conv.taps.resolve(approval_id, approved):
        outcome = f"{verdict} — the agent is carrying on."
    else:
        from ..policy.replay import deny_approval, execute_approved

        if approved:
            ok, text = await execute_approved(
                approval_id, origin="telegram", note="approved by telegram tap"
            )
            outcome = "Approved and run." if ok else f"Not run: {text}"
        else:
            ok, text = await deny_approval(approval_id, note="denied by telegram tap")
            outcome = text if ok else f"Not denied: {text}"
    await ack(verdict)
    if message_id is not None:
        original = str(message.get("text") or "")
        with contextlib.suppress(Exception):
            await call(
                cfg, client, "editMessageText", chat_id=chat_id, message_id=message_id,
                text=f"{original}\n\n{outcome}"[: cfg.telegram.max_message_chars],
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
        self.taps = Taps()
        # One turn at a time per chat. The poll loop no longer blocks on a turn - it cannot,
        # or the tap that turn is waiting for would never be read - so ordering within a
        # chat has to be kept here instead.
        self.locks: dict[int, asyncio.Lock] = {}

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(chat_id, asyncio.Lock())

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
        await say(cfg, client, chat_id, await approve(rest, taps=conv.taps))
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


async def approve(approval_id: str, *, taps: Taps | None = None) -> str:
    """Say yes from the phone, and mean it.

    Shares `execute_approved` with the CLI rather than reimplementing consent. Marking a row
    approved without running it would be worse than doing nothing: the CLI returns early on
    anything that is not `pending`, so a half-approval here would strand the action forever.

    If a turn is still waiting on this very id, the typed command is the tap: the turn runs
    the call itself, once. Replaying it here as well would run it twice.
    """
    if not approval_id:
        return "Which one? /approvals lists them."
    try:
        target = UUID(approval_id)
    except ValueError:
        return "That is not an approval id."
    if taps is not None and taps.resolve(target, True):
        return "Approved — the agent is carrying on."

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
    loop = AgentLoop(cfg=cfg, approver=TelegramApprover(cfg, client, chat_id, conv.taps))
    await call(cfg, client, "sendChatAction", chat_id=chat_id, action="typing")

    answer = ""
    denied: list[str] = []
    progress = Progress(cfg, client, chat_id, show=conv.show_tools(chat_id))

    # The run is named up front: the progress message is a subscriber to this run's journal,
    # and a subscriber cannot filter on a run id the turn has not opened yet. Which tools ran
    # is read from the journal here exactly as it is in the REPL - one feed, two surfaces.
    run_id = str(uuid7())
    tail = JournalTail.local(run_id=run_id, cfg=cfg)
    turn_over = asyncio.Event()

    async def absorb(event: Event) -> None:
        payload = event.payload
        if event.type == "tool_requested":
            await progress.started(payload["name"], payload["args"])
        elif event.type == "tool_finished":
            await progress.finished(payload["name"], ok=True, denied=False)
        elif event.type == "tool_failed":
            refused = bool(payload["denied"])
            await progress.finished(payload["name"], ok=False, denied=refused)
            if refused:
                denied.append(payload["name"])

    async def watch() -> None:
        async for event in tail.follow(stop=turn_over, until=turn_ended(run_id)):
            await absorb(event)

    async def run() -> None:
        nonlocal answer
        async for event in loop.run_turn(
            session, text, origin="telegram", autonomy=cfg.telegram.autonomy, run_id=run_id
        ):
            if isinstance(event, Answer):
                answer = event.text

    task = asyncio.create_task(run())
    watcher = asyncio.create_task(watch())
    while not task.done():
        # A silent bot is indistinguishable from a broken one, and a local model on a busy
        # GPU is often neither.
        done, _ = await asyncio.wait({task}, timeout=cfg.telegram.slow_turn_s)
        if not done:
            await call(cfg, client, "sendChatAction", chat_id=chat_id, action="typing")
    await task
    turn_over.set()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await watcher
    # The tail of the run can land after the watcher stopped; a cursor read cannot miss it.
    for event in tail.drain():
        await absorb(event)

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
    turns: set[asyncio.Task] = set()

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
                    allowed_updates=["message", "callback_query"],
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
                query = update.get("callback_query")
                if query is not None:
                    # A tap. Same boundary as a message: the chat the button sits in must
                    # be an allowed one, or a stranger who found the bot could approve a
                    # write by guessing an id.
                    tap_chat = int(((query.get("message") or {}).get("chat") or {}).get("id", 0))
                    if tap_chat not in allowed:
                        await refuse_stranger(tap_chat, strangers)
                        continue
                    with contextlib.suppress(Exception):
                        await handle_tap(cfg, client, conv, query)
                    continue
                message = update.get("message") or {}
                chat_id = int((message.get("chat") or {}).get("id", 0))
                if chat_id not in allowed:
                    await refuse_stranger(chat_id, strangers)
                    continue
                # Not awaited here. A turn may now block on an approval tap, and the tap
                # arrives through this very loop - a loop that waited for the turn would be
                # waiting for itself. `attend` keeps one turn at a time per chat.
                task = asyncio.create_task(attend(cfg, client, conv, message))
                turns.add(task)
                task.add_done_callback(turns.discard)

        for task in list(turns):
            task.cancel()
        await asyncio.gather(*turns, return_exceptions=True)


async def attend(
    cfg: Config, client: httpx.AsyncClient, conv: Conversation, message: dict
) -> None:
    """One update, off the poll loop, with the error handling the loop used to do inline.

    Commands skip the per-chat lock: `/approve <id>` typed while a turn waits for exactly
    that approval must not queue behind the turn it would release.
    """
    chat_id = int((message.get("chat") or {}).get("id", 0))
    text = str(message.get("text") or "").strip()
    try:
        if text.startswith("/"):
            await handle_message(cfg, client, conv, message)
            return
        async with conv.lock(chat_id):
            await handle_message(cfg, client, conv, message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        with contextlib.suppress(Exception):
            await say(cfg, client, chat_id, f"That went wrong: {type(exc).__name__}")
        await repo_ops.write_action(
            repo_ops.ActionRecord(
                actor="channel:telegram", kind="telegram_message", name="turn",
                status="error", error=str(exc)[:300],
            )
        )
