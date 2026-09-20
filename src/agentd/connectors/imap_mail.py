"""IMAP: the mailboxes that have no API, which is most of them.

dodds.org is hosted on Tucows' OpenSRS platform, which speaks IMAP and nothing else - there
is no OAuth to do, so the credential is a password and the honest thing is to say so. It
lives in the vault like every other credential, and the agent is fenced out of it the same
three ways; what it is not is scoped. A mail password is the whole mailbox, and unlike
Google's `gmail.readonly` there is no server-side proof that this daemon only reads.

So the read-only guarantee here is structural instead, and doubled on purpose:

- `SELECT` is issued with `readonly=True`, which is `EXAMINE`: the server itself refuses to
  set `\\Seen` for this session.
- Headers are fetched with `BODY.PEEK[...]`, the form that explicitly does not mark a
  message read, so even a server that ignored `EXAMINE` would leave your unread count alone.

**This connector ignores the `httpx.AsyncClient` it is handed**, which is the one place in
the framework where that is true. IMAP is a stateful TLS socket, not a request; `imaplib`
is synchronous, so the whole conversation happens in a worker thread via `asyncio.to_thread`
and the event loop keeps running. The cost is that `MockTransport` cannot drive this file -
the tests inject a fake fetcher instead, which is why `_fetch` is a separate function and
not a method body.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
from dataclasses import dataclass
from datetime import timedelta
from email import policy
from typing import Any
from uuid import UUID

import httpx

from .. import secrets as vault
from ..config import Config, ImapAccountConfig
from ..db import repo_agenda
from ..ids import utcnow
from . import mail
from .base import (
    Connector,
    ConnectorAuthError,
    ConnectorTransient,
    Item,
    PollResult,
    tracked_loops,
)

HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED"
UID_IN_RESPONSE = re.compile(rb"UID (\d+)")
MAX_TRACKED_SENDERS = 500


@dataclass
class Fetched:
    uid: str
    uid_validity: str
    headers: dict[str, str]


def _fetch(
    account: ImapAccountConfig, password: str, uids: list[str] | None, timeout_s: float
) -> tuple[list[str], str, list[Fetched]]:
    """One IMAP conversation, synchronously. Returns (all unseen uids, uidvalidity, fetched).

    Two round trips: a UID SEARCH for what is unread, then one UID FETCH for the headers the
    caller asked for. Passing `uids=None` does the search only, which is what the sweep
    needs - it wants the *set*, not the contents.
    """
    try:
        conn = imaplib.IMAP4_SSL(account.host, account.port, timeout=timeout_s)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ConnectorTransient(f"connect: {type(exc).__name__}") from exc

    try:
        try:
            conn.login(account.username, password)
        except imaplib.IMAP4.error as exc:
            # imaplib puts the server's text in the exception; it names the failure without
            # quoting the password, and this is the one failure a human must act on.
            raise ConnectorAuthError(f"{account.username}: login refused ({exc})") from exc

        # readonly=True is EXAMINE. The server will not set \Seen for this session.
        status, data = conn.select(account.mailbox, readonly=True)
        if status != "OK":
            raise ConnectorTransient(f"cannot open {account.mailbox}")
        uid_validity = _uid_validity(conn)

        status, data = conn.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise ConnectorTransient("UID SEARCH failed")
        all_uids = [b.decode("ascii", "ignore") for b in (data[0] or b"").split()]

        if uids is None or not uids:
            return all_uids, uid_validity, []

        status, data = conn.uid(
            "FETCH", ",".join(uids), f"(BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
        )
        if status != "OK":
            raise ConnectorTransient("UID FETCH failed")
        return all_uids, uid_validity, _parse_fetch(data, uid_validity)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ConnectorTransient(f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _uid_validity(conn: imaplib.IMAP4_SSL) -> str:
    """UIDs are only unique within a UIDVALIDITY. Prefixing with it is what stops a mailbox
    that was rebuilt server-side from colliding with everything already in the archive."""
    try:
        status, data = conn.response("UIDVALIDITY")
        if status == "UIDVALIDITY" and data and data[0]:
            return data[0].decode("ascii", "ignore")
    except Exception:
        pass
    return "0"


def _parse_fetch(data: list, uid_validity: str) -> list[Fetched]:
    out: list[Fetched] = []
    for part in data:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        prefix, raw = part[0], part[1]
        match = UID_IN_RESPONSE.search(prefix or b"")
        if not match or not isinstance(raw, bytes):
            continue
        out.append(
            Fetched(
                uid=match.group(1).decode("ascii"),
                uid_validity=uid_validity,
                headers=_headers(raw),
            )
        )
    return out


def _headers(raw: bytes) -> dict[str, str]:
    """Decoded header values. `policy.default` unfolds and decodes RFC 2047 encoded words,
    so a subject in Chinese arrives as text rather than `=?utf-8?B?...?=` - and a header so
    malformed that it raises falls back to the legacy parser rather than losing the message.
    """
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
        return {k: str(v) for k, v in message.items()}
    except Exception:
        message = email.message_from_bytes(raw)
        return {k: str(v) for k, v in message.items()}


class ImapConnector(Connector):
    name = "imap"

    def __init__(self, cfg: Config, label: str) -> None:
        self.cfg = cfg
        self.label = label
        self.account = cfg.connectors.imap.accounts[label]
        self.name = f"imap-{label}"
        self.poll_interval_s = self.account.poll_interval_s
        self.sweep_interval_s = self.account.sweep_interval_s
        # Overridden in tests: see the module docstring for why this is not a MockTransport.
        self.fetcher = _fetch

    @property
    def vault_ref(self) -> str:
        return f"imap/{self.label}"

    def configured(self) -> str | None:
        where = f"[connectors.imap.accounts.{self.label}]"
        if not self.account.host:
            return f"set {where} host in config.toml"
        if not self.account.username:
            return f"set {where} username in config.toml"
        if vault.get(self.vault_ref, "password") is None:
            return f"no password: run `agent secrets set {self.vault_ref} password`"
        return None

    def me(self) -> set[str]:
        return {a.lower() for a in [self.account.me(), *self.account.rules.aliases] if a}

    def _password(self) -> str:
        secret = vault.get(self.vault_ref, "password")
        if secret is None:
            raise ConnectorAuthError(f"no password in the vault at {self.vault_ref}")
        return secret.reveal()

    async def _run(self, uids: list[str] | None) -> tuple[list[str], str, list[Fetched]]:
        return await asyncio.to_thread(
            self.fetcher, self.account, self._password(), uids, self.cfg.connectors.timeout_s
        )

    async def poll(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> PollResult:
        known: dict[str, str] = dict(cursor.get("senders") or {})
        all_uids, uid_validity, _ = await self._run(None)

        # Oldest first, so a backlog drains over successive polls instead of the same newest
        # handful being re-read forever.
        budget = self.cfg.connectors.max_items_per_poll
        wanted = [u for u in all_uids if _key(uid_validity, u) not in known]
        deferred = max(0, len(wanted) - budget)
        wanted = wanted[:budget]

        fetched: list[Fetched] = []
        if wanted:
            _all, uid_validity, fetched = await self._run(wanted)

        senders = {
            _key(uid_validity, u): known[_key(uid_validity, u)]
            for u in all_uids
            if _key(uid_validity, u) in known
        }
        items: list[Item] = []
        for message in fetched:
            item = _to_item(message, self.account.me(), self.account.mailbox)
            items.append(item)
            senders[item.external_id] = mail.sender_address(item.payload.get("From", ""))

        return PollResult(
            items=items,
            cursor={
                "senders": dict(list(senders.items())[:MAX_TRACKED_SENDERS]),
                "uid_validity": uid_validity,
                "truncated": bool(deferred),
            },
            note=f"{deferred} deferred to the next poll" if deferred else None,
        )

    async def react(self, item: Item, event_id: UUID) -> int:
        headers = mail.Headers.from_pairs(
            {k: v for k, v in item.payload.items() if isinstance(v, str)}
        )
        rules = self.account.rules
        if not mail.wants_reply(headers, self.me(), rules):
            return 0

        title = mail.compose_title(mail.sender_address(headers.sender), self.label)
        if await repo_agenda.loop_exists(title):
            return 0

        loop_id = await repo_agenda.add_open_loop(
            title=title,
            detail=mail.detail(headers, None, self.account.me()),
            due_at=utcnow() + timedelta(hours=rules.due_in_h) if rules.due_in_h else None,
            source_event_id=event_id,
        )
        if rules.notify:
            await repo_agenda.notify(
                source=f"connector:{self.name}",
                level="info",
                title=title,
                body="",
                ref={"loop": str(loop_id), "event": str(event_id)},
            )
        return 1

    async def sweep(self, client: httpx.AsyncClient) -> None:
        """Close loops for people with nothing unread from them any more. Your mail client
        marking a message read is the signal; this connector never produces it itself."""
        from ..db import repo_connectors

        state = await repo_connectors.load_state(self.name)
        cursor = dict(state.get("cursor") or {})
        if cursor.get("truncated"):
            return
        known: dict[str, str] = dict(cursor.get("senders") or {})

        all_uids, uid_validity, _ = await self._run(None)
        keys = [_key(uid_validity, u) for u in all_uids]
        if any(key not in known for key in keys):
            # Unread mail this connector has not read the headers of yet. The next poll will
            # resolve it; until then closing anything risks closing that very person.
            return

        active = {mail.compose_title(known[key], self.label) for key in keys}
        for row in await tracked_loops(self.name):
            if row["title"] not in active:
                await repo_agenda.close_open_loop(row["id"])


def _key(uid_validity: str, uid: str) -> str:
    return f"{uid_validity}:{uid}"


def _to_item(message: Fetched, address: str, mailbox: str) -> Item:
    headers = message.headers
    key = _key(message.uid_validity, message.uid)
    return Item(
        # The server's own identifier, not the sender's Message-ID: a Message-ID is written
        # by whoever sent the mail and two of them can collide, by accident or on purpose.
        external_id=key,
        # Headers of a delivered message do not change, so one version is the whole story.
        version="1",
        kind="message",
        title=headers.get("Subject", ""),
        body="",  # BODY.PEEK[HEADER.FIELDS] fetched no body, and none is needed
        actor=mail.sender_address(headers.get("From", "")),
        url=None,
        occurred_at=_date(headers.get("Date", "")),
        payload={**headers, "account": address, "mailbox": mailbox, "uid": message.uid},
    )


def _date(value: str):
    """A Date header is written by the sender's clock and may be missing, malformed or
    naive. A naive one is read as UTC rather than dropped, because being a few hours out in
    the archive beats losing when a message arrived."""
    from datetime import UTC
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
