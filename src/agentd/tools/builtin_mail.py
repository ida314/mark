"""Live Gmail reads: the first tool that hands the user's own private data to the model.

The Gmail connector and these tools answer different questions. The connector decides, on
the daemon's schedule, what is *waiting on you* — unread, addressed to you, not bulk — and
opens a loop per sender without the model ever seeing a word of it. These tools answer "what
did the registrar actually say", which needs the mailbox, not the archive: mail that was
read, archived, filtered out by the noise rules, or sent in 2024.

**The credential does not move.** A tool handler is Python in this process, not something
the model runs. It calls Google with a vault credential and returns text, exactly as the
connector does, and `gmail.readonly` cannot send, delete, label or mark read — enforced by
Google, not by this file's good intentions. What is genuinely new is that *the model chooses
what to fetch*, and it chooses while holding text a stranger wrote.

That is why these are the first tools to declare `private_output=True`. The flag raises
`session.private`, and the shipped policy hard-denies every tool tagged `egress`, every
write, and every MCP call for the rest of the session. Reading your mail closes the door
behind it. Two deliberate consequences:

- The interlock denies rather than asks. An approval prompt on every web call after reading
  mail is approval fatigue, and fatigue is how the one that matters gets waved through.
- It is sticky for the session, not the turn. The mail leaves the context window at the next
  turn, but what the model concluded from it does not.

Splitting search from fetch is the other half. A search returns headers and Gmail's own
one-line snippet; pulling a whole attacker-controlled document takes a second, deliberate
call naming an id the search just produced. Most questions never need one.
"""

from __future__ import annotations

import base64
import html as htmllib
import re
from typing import Any

import httpx

from ..config import Config
from ..connectors import google_auth
from ..connectors.base import ConnectorAuthError, ConnectorRateLimited, ConnectorTransient
from ..connectors.gmail import METADATA_HEADERS
from .base import ToolContext, ToolResult, flat, obj, required, tool

MAX_RESULTS = 25
DEFAULT_RESULTS = 10
SNIPPET_CHARS = 160
# Under cfg.agent.tool_result_max_chars (8000) on purpose: the executor truncates the whole
# result *before* wrapping it, so a body that filled the budget would push the headers out
# and leave the `<untrusted_content>` frame wrapping nothing but somebody else's prose.
MAX_BODY_CHARS = 6000
HTML_TAG = re.compile(r"<[^>]+>")


# --- accounts ----------------------------------------------------------------


def resolve_account(cfg: Config, label: str | None) -> tuple[str, str] | str:
    """(label, address) for the account to query, or a sentence explaining why not.

    Returning prose rather than raising is the convention here: the model reads the error
    and can fix the call itself, which for "which mailbox did you mean" it usually can.
    """
    accounts = cfg.connectors.google.accounts
    if not accounts:
        return "No Google accounts are configured; see [connectors.google] in config.toml."

    authorised = [
        (name, account.address)
        for name, account in sorted(accounts.items())
        if account.mail and account.address
        and google_auth.not_configured(account.address) is None
    ]
    if label:
        for name, address in authorised:
            if name == label:
                return name, address
        known = ", ".join(name for name, _ in authorised) or "none"
        return f"No authorised mail account labelled {label!r}. Authorised: {known}."
    if not authorised:
        return (
            "No mail account is authorised yet. Run `agent connectors auth <label>` for one "
            "of: " + (", ".join(sorted(accounts)) or "none configured") + "."
        )
    if len(authorised) > 1:
        names = ", ".join(name for name, _ in authorised)
        return f"Several mail accounts are authorised ({names}); say which one."
    return authorised[0]


# --- requests ----------------------------------------------------------------


async def fetch(
    cfg: Config,
    address: str,
    path: str,
    params: list[tuple[str, str]],
    client: httpx.AsyncClient | None = None,
) -> dict:
    """One authenticated Gmail GET.

    `client` is injectable so the HTTP paths can be driven by `httpx.MockTransport` in a
    test, the way every connector already is. No other builtin tool offers that seam, and
    the absence is why `web_fetch` has no test beyond its URL guard.
    """
    url = f"{cfg.connectors.google.gmail_api_base.rstrip('/')}/gmail/v1/users/me/{path}"
    if client is not None:
        return await google_auth.get_json(
            cfg, client, address=address, url=url, params=params, what="Gmail"
        )
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as owned:
        return await google_auth.get_json(
            cfg, owned, address=address, url=url, params=params, what="Gmail"
        )


def explain(exc: Exception) -> str:
    """Their failure, in a sentence the model can act on and without the credential in it."""
    if isinstance(exc, ConnectorAuthError):
        return f"Gmail refused the credential: {exc}"
    if isinstance(exc, ConnectorRateLimited):
        return f"Gmail is rate limiting; try again in about {exc.retry_after_s:.0f}s."
    if isinstance(exc, ConnectorTransient):
        return f"Gmail is not reachable right now: {exc}"
    return f"{type(exc).__name__}: {exc}"


# --- decoding ----------------------------------------------------------------


def b64(data: str) -> bytes:
    """Gmail's base64url, which arrives without padding as often as with it."""
    if not data:
        return b""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return b""


def charset_of(part: dict) -> str:
    for header in part.get("headers") or []:
        if str(header.get("name", "")).lower() == "content-type":
            match = re.search(r"charset=\"?([\w\-]+)\"?", str(header.get("value", "")), re.I)
            if match:
                return match.group(1)
    return "utf-8"


def strip_html(markup: str) -> str:
    """text/html when there is no text/plain alternative.

    `trafilatura` is already a dependency and is what `web_fetch` uses, so it is what a
    reader of this codebase expects; the regex is the floor it falls to, because losing the
    body entirely is worse than losing its formatting.
    """
    try:
        import trafilatura

        extracted = trafilatura.extract(markup, include_links=False, include_comments=False)
        if extracted:
            return extracted
    except Exception:
        pass
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    return re.sub(r"\n{3,}", "\n\n", htmllib.unescape(HTML_TAG.sub(" ", text))).strip()


def walk(payload: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """(text parts, attachment filenames) from Gmail's MIME tree, depth first.

    Attachments are never downloaded — only named. Their bytes would be a second, much
    larger untrusted document, and nothing here can do anything useful with a PDF anyway.
    """
    texts: list[tuple[str, str]] = []
    attachments: list[str] = []

    def visit(part: Any, depth: int = 0) -> None:
        if not isinstance(part, dict) or depth > 12:
            return
        filename = str(part.get("filename") or "")
        mime = str(part.get("mimeType") or "")
        if filename:
            attachments.append(filename[:120])
        elif mime.startswith("text/"):
            raw = b64(str((part.get("body") or {}).get("data") or ""))
            if raw:
                texts.append((mime, raw.decode(charset_of(part), errors="replace")))
        for child in part.get("parts") or []:
            visit(child, depth + 1)

    visit(payload)
    return texts, attachments


def best_body(payload: dict) -> tuple[str, list[str]]:
    """The most readable text in the message, and what was attached to it."""
    texts, attachments = walk(payload)
    for mime, text in texts:
        if mime.startswith("text/plain") and text.strip():
            return text.strip(), attachments
    for mime, text in texts:
        if mime.startswith("text/html") and text.strip():
            return strip_html(text), attachments
    return (texts[0][1].strip() if texts else ""), attachments


def headers_of(message: dict) -> dict[str, str]:
    return {
        str(h.get("name")): str(h.get("value") or "")
        for h in (message.get("payload") or {}).get("headers") or []
        if isinstance(h, dict) and h.get("name")
    }


def one_line(message: dict) -> str:
    """One search hit, in the house style: id in brackets so it can be passed on."""
    h = headers_of(message)
    date = flat(h.get("Date", ""), 31)
    sender = flat(h.get("From", ""), 80)
    subject = flat(h.get("Subject", "") or "(no subject)", 120)
    snippet = flat(message.get("snippet") or "", SNIPPET_CHARS)
    line = f"- {date} · {sender} — {subject} [{message.get('id')}]"
    return line + (f"\n    {snippet}" if snippet else "")


# --- the tools ---------------------------------------------------------------


@tool(
    "gmail_search",
    "Search the user's Gmail and return matching messages: sender, date, subject and a "
    "one-line preview. Accepts full Gmail search syntax, for example "
    "'from:registrar@nyu.edu newer_than:30d' or 'has:attachment invoice'. Returns message "
    "ids; pass one to gmail_message to read the whole thing.",
    required(
        obj(
            query={"type": "string", "description": "Gmail search syntax"},
            account={"type": "string", "description": "account label, e.g. nyu"},
            limit={"type": "integer", "description": f"1-{MAX_RESULTS}, default {DEFAULT_RESULTS}"},
        ),
        "query",
    ),
    tags=("mail", "untrusted"),
    always_on=True,
    trust_output=False,
    private_output=True,
)
async def gmail_search(args: dict, ctx: ToolContext) -> ToolResult:
    from ..config import get_config

    cfg = get_config()
    account = resolve_account(cfg, args.get("account"))
    if isinstance(account, str):
        return ToolResult(content=account, ok=False)
    label, address = account

    limit = max(1, min(int(args.get("limit", DEFAULT_RESULTS)), MAX_RESULTS))
    try:
        listing = await fetch(
            cfg, address, "messages",
            [("q", str(args["query"])), ("maxResults", str(limit))],
        )
        ids = [
            str(m["id"])
            for m in (listing.get("messages") or [])
            if isinstance(m, dict) and m.get("id")
        ][:limit]
        params = [("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS]
        messages = [await fetch(cfg, address, f"messages/{mid}", params) for mid in ids]
    except Exception as exc:
        return ToolResult(content=explain(exc), ok=False)

    if not messages:
        return ToolResult(content="No messages match that search.", trust="untrusted")
    body = "\n".join(one_line(m) for m in messages)
    return ToolResult(
        content=f"{len(messages)} message(s) in {label} ({address}):\n{body}",
        trust="untrusted",
        data={"count": len(messages), "account": label},
    )


@tool(
    "gmail_message",
    "Read one full Gmail message by its id, as returned by gmail_search. Gives the headers "
    "and the decoded text body. Attachments are named, never downloaded.",
    required(
        obj(
            id={"type": "string", "description": "message id from gmail_search"},
            account={"type": "string", "description": "account label, e.g. nyu"},
        ),
        "id",
    ),
    tags=("mail", "untrusted"),
    always_on=True,
    trust_output=False,
    private_output=True,
)
async def gmail_message(args: dict, ctx: ToolContext) -> ToolResult:
    from ..config import get_config

    cfg = get_config()
    account = resolve_account(cfg, args.get("account"))
    if isinstance(account, str):
        return ToolResult(content=account, ok=False)
    _label, address = account

    try:
        message = await fetch(cfg, address, f"messages/{args['id']}", [("format", "full")])
    except Exception as exc:
        return ToolResult(content=explain(exc), ok=False)

    h = headers_of(message)
    text, attachments = best_body(message.get("payload") or {})
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n... [body truncated, {len(text)} chars in total]"

    lines = [
        f"From: {h.get('From', '')}",
        f"To: {h.get('To', '')}",
        f"Date: {h.get('Date', '')}",
        f"Subject: {h.get('Subject', '(no subject)')}",
    ]
    if h.get("Cc"):
        lines.insert(2, f"Cc: {h['Cc']}")
    if attachments:
        lines.append(f"Attachments (not downloaded): {', '.join(attachments)}")
    return ToolResult(
        content="\n".join(lines) + "\n\n" + (text or "(no readable text body)"),
        trust="untrusted",
        data={"attachments": len(attachments)},
    )


TOOLS = [gmail_search, gmail_message]
