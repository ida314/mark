"""Gmail: mail that is waiting on you becomes an open loop.

Two endpoints. `messages.list` with a query answers "what is unread in the inbox" in one
request, and `messages.get?format=metadata` answers "who is it from" for the ones we have
not already looked at. Which ids those are is the cursor, so a steady-state poll of a
mailbox where nothing has changed costs exactly one request.

Not `history.list`, which is the incremental API and looks like the obvious choice. It
needs a baseline `historyId` that expires if the daemon is off for long enough, it reports
label changes rather than the current state, and recovering from an expired id means a full
listing anyway. The cost of being wrong about that is silently missing mail, which is the
worst failure this connector has; a listing cannot miss anything.

Strictly read-only, enforced by the scope rather than by our own care: `gmail.readonly`
cannot mark a message read, cannot send, cannot delete. Your unread count is untouched by
the fact that the daemon has seen a message, which also means the source of truth for
"still waiting" stays your own inbox - which is what makes the sweep meaningful.

One loop per sender per account. See `mail.py` for why, and for the rule that their subject
line never reaches a title.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from ..config import Config
from ..db import repo_agenda
from ..ids import utcnow
from . import google_auth, mail
from .base import Connector, ConnectorTransient, Item, PollResult, tracked_loops

METADATA_HEADERS = [
    "From", "To", "Cc", "Subject", "Date", "Message-ID",
    "List-Id", "List-Unsubscribe", "Precedence", "Auto-Submitted",
]
# Enough to notice a mailbox nobody triages; beyond this the sweep stops closing loops
# rather than close one on the strength of a listing it knows is partial.
MAX_TRACKED_SENDERS = 500


class GmailConnector(Connector):
    name = "gmail"

    def __init__(self, cfg: Config, label: str) -> None:
        self.cfg = cfg
        self.label = label
        self.account = cfg.connectors.google.accounts[label]
        self.name = f"gmail-{label}"
        self.poll_interval_s = self.account.poll_interval_s
        self.sweep_interval_s = self.account.sweep_interval_s

    # --- credentials ---------------------------------------------------------

    @property
    def vault_ref(self) -> str:
        return google_auth.account_ref(self.account.address)

    def configured(self) -> str | None:
        why = google_auth.not_configured(self.account.address)
        if why and "auth <label>" in why:
            return why.replace("auth <label>", f"auth {self.label}")
        return why

    def me(self) -> set[str]:
        return {a.lower() for a in [self.account.address, *self.account.rules.aliases] if a}

    # --- requests ------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.cfg.connectors.google.gmail_api_base.rstrip('/')}/gmail/v1/users/me/{path}"

    async def _get(
        self, client: httpx.AsyncClient, path: str, params: list[tuple[str, str]]
    ) -> dict:
        return await google_auth.get_json(
            self.cfg,
            client,
            address=self.account.address,
            url=self._url(path),
            params=params,
            what="Gmail",
        )

    async def _list_unread(self, client: httpx.AsyncClient) -> tuple[list[str], bool]:
        body = await self._get(
            client,
            "messages",
            [("q", self.account.query), ("maxResults", str(self.account.max_results))],
        )
        messages = body.get("messages") or []
        if not isinstance(messages, list):
            raise ConnectorTransient("unexpected messages.list shape")
        ids = [str(m.get("id")) for m in messages if isinstance(m, dict) and m.get("id")]
        return ids, bool(body.get("nextPageToken"))

    async def _metadata(self, client: httpx.AsyncClient, message_id: str) -> dict:
        params = [("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS]
        return await self._get(client, f"messages/{message_id}", params)

    # --- polling -------------------------------------------------------------

    async def poll(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> PollResult:
        ids, truncated = await self._list_unread(client)
        known: dict[str, str] = dict(cursor.get("senders") or {})
        # `poll_once` archives at most `max_items_per_poll` of what we return, so fetching
        # more than that would record mail in the cursor as seen while dropping it on the
        # floor. Anything over the budget is simply left out of the cursor and picked up by
        # the next poll — the one failure mode worth spending a round trip to avoid.
        budget = self.cfg.connectors.max_items_per_poll

        items: list[Item] = []
        senders: dict[str, str] = {}
        deferred = 0
        for message_id in ids:
            if message_id in known:
                # Already archived on an earlier poll. Carry its sender forward so the sweep
                # still knows who is waiting without re-fetching the whole mailbox.
                senders[message_id] = known[message_id]
                continue
            if len(items) >= budget:
                deferred += 1
                continue
            item = _to_item(await self._metadata(client, message_id), self.account.address)
            items.append(item)
            senders[message_id] = mail.sender_address(item.payload.get("From", ""))

        note = f"{deferred} deferred to the next poll" if deferred else None
        return PollResult(
            items=items,
            # Replaced wholesale, so a message that left the unread set drops out of the
            # cursor on its own and would be re-fetched if it ever came back.
            cursor={
                "senders": dict(list(senders.items())[:MAX_TRACKED_SENDERS]),
                # Either way the sweep is looking at a partial view of the mailbox and must
                # not close anything on the strength of it.
                "truncated": truncated or bool(deferred),
            },
            note=note or ("nothing new" if not items else None),
        )

    # --- reacting ------------------------------------------------------------

    async def react(self, item: Item, event_id: UUID) -> int:
        headers = mail.Headers.from_pairs(
            {k: v for k, v in item.payload.items() if isinstance(v, str)}
        )
        rules = self.account.rules
        if not mail.wants_reply(headers, self.me(), rules):
            return 0

        title = mail.compose_title(mail.sender_address(headers.sender), self.label)
        if await repo_agenda.loop_exists(title):
            # They already have your attention. A second message is archived, not announced.
            return 0

        loop_id = await repo_agenda.add_open_loop(
            title=title,
            detail=mail.detail(headers, item.url, self.account.address),
            due_at=utcnow() + timedelta(hours=rules.due_in_h) if rules.due_in_h else None,
            source_event_id=event_id,
            # No waiting_on: that status means waiting on someone else. This is on you.
        )
        if rules.notify:
            await repo_agenda.notify(
                source=f"connector:{self.name}",
                level="info",
                title=title,
                body=item.url or "",
                ref={"loop": str(loop_id), "event": str(event_id), "url": item.url},
            )
        return 1

    # --- closing what is finished --------------------------------------------

    async def sweep(self, client: httpx.AsyncClient) -> None:
        """Close loops for people with nothing unread from them any more.

        You read it on your phone, or you replied and archived it: either way your inbox is
        the source of truth about whether someone is still waiting, and this is the only
        place that reads it. A partial listing closes nothing, because "not on the page I
        was shown" and "not in the mailbox" are different facts.
        """
        ids, truncated = await self._list_unread(client)
        state = await _cursor(self.name)
        if truncated or state.get("truncated"):
            return

        known: dict[str, str] = dict(state.get("senders") or {})
        if any(message_id not in known for message_id in ids):
            # Something unread that this connector has never looked at. Until the next poll
            # resolves who it is from, closing anything risks closing that very person.
            return

        active = {mail.compose_title(known[i], self.label) for i in ids}
        for row in await tracked_loops(self.name):
            if row["title"] not in active:
                await repo_agenda.close_open_loop(row["id"])


# --- parsing -----------------------------------------------------------------


def _to_item(message: dict, address: str) -> Item:
    headers = {
        str(h.get("name")): str(h.get("value") or "")
        for h in (message.get("payload") or {}).get("headers") or []
        if isinstance(h, dict) and h.get("name")
    }
    message_id = str(message.get("id") or "")
    return Item(
        external_id=message_id,
        # internalDate is Gmail's own receipt time and never changes for a given message,
        # so a message that stays unread for a week is archived once, not once per poll.
        version=str(message.get("internalDate") or ""),
        kind="message",
        title=headers.get("Subject", ""),
        body=str(message.get("snippet") or ""),
        actor=mail.sender_address(headers.get("From", "")),
        url=f"https://mail.google.com/mail/u/?authuser={address}#all/{message_id}",
        occurred_at=_internal_date(message.get("internalDate")),
        payload={**headers, "thread_id": message.get("threadId"), "account": address,
                 "label_ids": message.get("labelIds")},
    )


def _internal_date(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


async def _cursor(name: str) -> dict:
    from ..db import repo_connectors

    state = await repo_connectors.load_state(name)
    return dict(state.get("cursor") or {})
