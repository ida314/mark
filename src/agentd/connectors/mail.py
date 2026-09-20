"""What the two mail connectors agree on: what counts as waiting on you, and what we say.

Gmail and IMAP fetch completely differently - one is a conditional HTTPS GET with an OAuth
bearer token, the other a TLS socket and a LOGIN - but the question they ask afterwards is
identical, so it is asked in exactly one place. Changing your noise budget should never mean
changing it twice and discovering later that the two drifted.

**This is the most hostile input in the system.** A GitHub notification at least comes from
someone with an account; an email comes from anyone who can send one, and every field in it
- the display name, the subject, the body, the Message-ID - is theirs. So the rule the
GitHub connector established is applied here with no exceptions:

- An open-loop title is a sentence *this file* composes. The only variable parts are an
  account label that came out of your own config.toml, and a sender address that fully
  matched `ADDRESS` - a charset with no whitespace, no quotes, no newlines and no colons.
  An address that does not match is replaced by "someone", not escaped.
- Their subject line goes in `detail`, which nothing renders to the model.

One loop per sender per account, not one per message. Five emails from the same person
while you have not replied is one thing waiting on you, and the sweep closes it when
nothing from them is unread any more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import getaddresses

# Deliberately stricter than RFC 5322, which permits quoted local parts containing almost
# anything. A title is a regular language we can state, and this is that statement.
ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,8}")
MAX_TITLE_ADDRESS = 96

LOOP_TITLE = "Reply to {who} ({account})"
BULK_PRECEDENCE = {"bulk", "list", "junk", "auto_reply"}


@dataclass(frozen=True)
class Headers:
    """The only parts of a message either connector reads. No body is ever fetched: the
    decision below does not need one, and not fetching it keeps the archive small and the
    attack surface a fixed set of header fields."""

    sender: str = ""  # raw From, display name and all
    to: str = ""
    cc: str = ""
    subject: str = ""
    date: str = ""
    message_id: str = ""
    list_id: str = ""
    list_unsubscribe: str = ""
    precedence: str = ""
    auto_submitted: str = ""

    @classmethod
    def from_pairs(cls, pairs: dict[str, str]) -> Headers:
        """Case-insensitive lookup, because Gmail returns `From` and IMAP servers return
        whatever the sending client wrote."""
        lower = {k.lower(): v for k, v in pairs.items()}
        return cls(
            sender=lower.get("from", ""),
            to=lower.get("to", ""),
            cc=lower.get("cc", ""),
            subject=lower.get("subject", ""),
            date=lower.get("date", ""),
            message_id=lower.get("message-id", ""),
            list_id=lower.get("list-id", ""),
            list_unsubscribe=lower.get("list-unsubscribe", ""),
            precedence=lower.get("precedence", ""),
            auto_submitted=lower.get("auto-submitted", ""),
        )


def sender_address(header: str) -> str:
    """The address out of a From header, lowercased. `getaddresses` handles the display-name
    forms; anything it cannot parse yields "", which fails every check below closed."""
    for _name, addr in getaddresses([header or ""]):
        if addr:
            return addr.strip().lower()
    return ""


def recipients(*headers: str) -> set[str]:
    return {addr.strip().lower() for _n, addr in getaddresses([h or "" for h in headers]) if addr}


def is_bulk(headers: Headers) -> bool:
    """Newsletters, mailing lists, receipts and out-of-office replies.

    Four independent signals, any of which is enough. They are all headers a sender sets
    about itself, so this is a courtesy the well-behaved extend and not a guarantee - which
    is exactly why `skip_senders` exists for the ones that lie.
    """
    if headers.list_unsubscribe or headers.list_id:
        return True
    if headers.precedence.strip().lower() in BULK_PRECEDENCE:
        return True
    auto = headers.auto_submitted.strip().lower()
    return bool(auto and not auto.startswith("no"))


def addressed_to_me(headers: Headers, me: set[str]) -> bool:
    return bool(recipients(headers.to, headers.cc) & me)


def sender_skipped(address: str, patterns: list[str]) -> bool:
    """An exact address, or "@domain" for everything from one domain."""
    address = address.lower()
    for raw in patterns:
        pattern = raw.strip().lower()
        if not pattern:
            continue
        if pattern.startswith("@"):
            if address.endswith(pattern):
                return True
        elif address == pattern:
            return True
    return False


def wants_reply(headers: Headers, me: set[str], rules) -> bool:
    """The whole noise budget, in one function so the two connectors cannot disagree.

    "Unread" and "in the inbox" are not checked here: both sources are asked for exactly
    that set, and re-deriving it from headers would be guessing at something the server
    already knows.
    """
    address = sender_address(headers.sender)
    if not address:
        return False
    if address in me:
        return False  # your own sent copy, or a list echoing you back to yourself
    if sender_skipped(address, rules.skip_senders):
        return False
    if rules.skip_bulk and is_bulk(headers):
        return False
    if rules.direct_only and not addressed_to_me(headers, me):
        return False
    return True


def compose_title(address: str, account_label: str) -> str:
    """Our sentence about their data. See the module docstring for why this shape.

    `account_label` is a config.toml key that `CONNECTOR_LABEL` already constrained to
    `[a-z0-9_-]`, so it cannot carry anything either. The address is the only field an
    outsider influences, and a non-matching one is replaced rather than repaired.
    """
    who = address if ADDRESS.fullmatch(address or "") and len(address) <= MAX_TITLE_ADDRESS else None
    return LOOP_TITLE.format(who=who or "someone", account=account_label)


def detail(headers: Headers, url: str | None, account: str) -> str:
    """Where their words live. `agent loops show <id>` is how a human reads this; no tool
    renders it to the model, which is the only reason a subject line may appear at all."""
    lines = [f"account: {account}", f"from: {headers.sender[:200]}"]
    if headers.subject:
        lines.append(f"subject: {headers.subject[:500]}")
    if headers.date:
        lines.append(f"date: {headers.date[:100]}")
    if url:
        lines.append(url)
    return "\n".join(lines)
