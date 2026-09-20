"""GitHub: review requests and mentions become things waiting on you.

One endpoint, `GET /notifications`, because its `reason` field already distinguishes a review
request from a mention from an assignment, a 304 costs nothing against the rate limit, and
`X-Poll-Interval` is GitHub telling you how often it is willing to be asked. Polling every
minute this way is effectively free.

Two honest limitations, stated here rather than discovered later:

- It reads the **unread** feed. A review request you dismissed on your phone never arrives.
  The fix would be `/search/issues`, which is a second cursor, a second rate limit and a
  second response shape; deferred deliberately.
- It is strictly **read-only**. It never marks anything read, so your GitHub inbox is
  untouched and a read-only token is sufficient — which is its own safety property.

The security-relevant part of this file is `compose_title`. Open-loop titles reach an LLM
prompt through `situation_report()`, so a title is model-visible input, and everything in one
is either a constant, a dictionary lookup, an integer, or a string that matched a very small
character class. The PR's own title — which anybody who can open a PR can write — goes into
`detail`, which no tool renders to the model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from .. import secrets as vault
from ..config import Config
from ..db import repo_agenda
from ..ids import utcnow
from .base import (
    Connector,
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
    Item,
    PollResult,
    safe_label,
    tracked_loops,
)

# Closed dictionaries: an unknown value can never reach a title verbatim.
SUBJECT_WORD = {
    "PullRequest": "PR",
    "Issue": "issue",
    "Discussion": "discussion",
    "Release": "release",
    "Commit": "commit",
    "RepositoryVulnerabilityAlert": "security alert",
}
LOOP_TITLES = {
    "review_requested": "Review requested: {what} in {repo}",
    "assign": "Assigned to you: {what} in {repo}",
    "mention": "Mentioned you: {what} in {repo}",
}
# GitHub's own charset for owner/repo. A name that does not match is replaced, not escaped.
REPO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,38}/[A-Za-z0-9._\-]{1,100}")
ITEM_NUMBER = re.compile(r"/(\d+)/?$")
PER_PAGE = 50


def _html_url(api_url: str | None) -> str | None:
    """Best-effort api.github.com -> github.com. Cosmetic: it only ever reaches `detail` and
    a notification body, never a title."""
    if not api_url:
        return None
    m = re.match(r"https://api\.github\.com/repos/([^/]+)/([^/]+)/(pulls|issues)/(\d+)$", api_url)
    if not m:
        return None
    owner, repo, kind, number = m.groups()
    return f"https://github.com/{owner}/{repo}/{'pull' if kind == 'pulls' else 'issues'}/{number}"


def compose_title(
    reason: str, subject_type: str, subject_url: str | None, repo_full_name: str | None
) -> str | None:
    """Our sentence about their data, or None when this reason opens no loop.

    Nothing an attacker controls is interpolated. The verb is a dict lookup keyed on
    `reason`; the noun is a dict lookup keyed on `subject.type`, defaulting to "thread"; the
    number is matched out of the URL and passed through `int()`, and an integer cannot carry
    an injection; the repository name is the one genuinely attacker-influenced field, and a
    name that does not match GitHub's own charset is *replaced* with "a repository".

    Returning None for an unknown reason is also how "should this open a loop?" is answered —
    one function, one decision, so the two can never disagree.
    """
    template = LOOP_TITLES.get(reason)
    if template is None:
        return None

    what = SUBJECT_WORD.get(subject_type, "thread")
    match = ITEM_NUMBER.search(subject_url or "")
    if match:
        what = f"{what} #{int(match.group(1))}"

    repo = repo_full_name if repo_full_name and REPO_NAME.fullmatch(repo_full_name) else None
    return template.format(what=what, repo=safe_label(repo, "a repository"))


def _retry_after(response: httpx.Response) -> float | None:
    """Distinguish 'you are going too fast' from 'you may not do this at all'.

    They arrive on the same status codes and mean opposite things: one is the source working
    as designed, the other needs a human to change a permission.
    """
    if response.headers.get("retry-after"):
        try:
            return float(response.headers["retry-after"])
        except ValueError:
            return 60.0
    if response.headers.get("x-ratelimit-remaining") == "0":
        try:
            reset = float(response.headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            return 60.0
        return max(1.0, reset - utcnow().timestamp())
    return None


class GithubConnector(Connector):
    name = "github"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.poll_interval_s = cfg.connectors.github.poll_interval_s
        self.sweep_interval_s = cfg.connectors.github.sweep_interval_s

    # --- credentials ---------------------------------------------------------

    @property
    def vault_ref(self) -> str:
        return f"github/{self.cfg.connectors.github.user}"

    def configured(self) -> str | None:
        if not self.cfg.connectors.github.user:
            return "set [connectors.github] user in config.toml"
        if vault.get(self.vault_ref, "token") is None:
            return f"no token: run `agent secrets set {self.vault_ref} token`"
        return None

    def _headers(self) -> dict[str, str]:
        """Built per request rather than held on a client, so a rotated token takes effect on
        the next poll (the vault is mtime-cached) and so a test can assert on the header."""
        token = vault.get(self.vault_ref, "token")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentd (personal agent daemon)",  # GitHub requires one
        }
        if token:
            headers["Authorization"] = f"Bearer {token.reveal()}"
        return headers

    # --- polling -------------------------------------------------------------

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise ConnectorAuthError("token rejected (401)")
        if response.status_code in (403, 429):
            retry = _retry_after(response)
            if retry is not None:
                raise ConnectorRateLimited(retry)
            raise ConnectorAuthError(
                "403 without rate-limit headers — the token most likely lacks the "
                "account-level 'Notifications' read permission"
            )
        if response.status_code == 404:
            # GitHub 404s rather than 403s for things a token may not see.
            raise ConnectorAuthError("404 — the token cannot see the notifications feed")
        if response.status_code >= 500:
            raise ConnectorTransient(f"GitHub returned {response.status_code}")

    async def _get(
        self, client: httpx.AsyncClient, *, etag: str | None = None
    ) -> httpx.Response:
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        url = f"{self.cfg.connectors.github.api_base.rstrip('/')}/notifications"
        try:
            response = await client.get(url, headers=headers, params={"per_page": PER_PAGE})
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ConnectorTransient(str(exc)) from exc
        self._check(response)
        return response

    async def poll(self, client: httpx.AsyncClient, cursor: dict[str, Any]) -> PollResult:
        response = await self._get(client, etag=cursor.get("etag"))
        hint = _poll_interval(response)

        if response.status_code == 304:
            # Nothing changed. Note what this does *not* mean: it says nothing about what
            # vanished, which is why closing loops is the sweep's job and never this one.
            return PollResult(items=[], cursor=cursor, next_poll_in_s=hint, note="not modified")

        threads = response.json()
        if not isinstance(threads, list):
            raise ConnectorTransient("unexpected response shape")

        if len(threads) >= PER_PAGE and 'rel="next"' in response.headers.get("link", ""):
            # Newest-first, so the tail is what gets dropped. Say so rather than lose it
            # quietly; a silent truncation is indistinguishable from nothing happening.
            await repo_agenda.notify(
                source="connector:github",
                level="warn",
                title="More unread GitHub notifications than one page",
                body=f"Ingested the newest {PER_PAGE}; older unread threads were not read.",
            )

        return PollResult(
            items=[_to_item(t) for t in threads],
            cursor={"etag": response.headers.get("ETag")},
            next_poll_in_s=hint,
        )

    # --- reacting ------------------------------------------------------------

    async def react(self, item: Item, event_id: UUID) -> int:
        gh = self.cfg.connectors.github
        reason = str(item.payload.get("reason", ""))
        if reason not in gh.include_reasons:
            return 0

        title = compose_title(
            reason,
            str(item.payload.get("subject_type", "")),
            item.payload.get("subject_url"),
            item.payload.get("repo"),
        )
        if title is None or await repo_agenda.loop_exists(title):
            return 0

        due = (
            utcnow() + timedelta(hours=gh.review_due_in_h)
            if gh.review_due_in_h and reason == "review_requested"
            else None
        )
        loop_id = await repo_agenda.add_open_loop(
            title=title,
            detail=_detail(item),
            due_at=due,
            source_event_id=event_id,
            # No waiting_on: add_open_loop derives status="waiting" from it, and "waiting"
            # means waiting on someone else. This is waiting on you.
        )
        if reason in gh.notify_reasons:
            # A push is a side effect of opening a loop, never of ingesting an event — which
            # is why five new comments on the same PR make five archive rows and no noise.
            await repo_agenda.notify(
                source="connector:github",
                level="info",
                title=title,
                body=item.payload.get("html_url") or "",
                ref={"loop": str(loop_id), "event": str(event_id),
                     "url": item.payload.get("html_url")},
            )
        return 1

    # --- closing what is finished --------------------------------------------

    async def sweep(self, client: httpx.AsyncClient) -> int:
        """Close loops whose thread has left your unread list.

        Deliberately unconditional — no `If-None-Match`. A 304 carries no information about
        what disappeared, so inferring closure from the cheap conditional poll would close
        every open loop the first quiet minute.
        """
        response = await self._get(client)
        threads = response.json()
        if not isinstance(threads, list):
            raise ConnectorTransient("unexpected response shape")

        active = {str(t.get("id")) for t in threads}
        # If the listing was truncated we only know about threads at least this recent;
        # anything older might be on a page we never saw, so it is not ours to close.
        watermark = (
            min((_parsed(t.get("updated_at")) for t in threads), default=None)
            if len(threads) >= PER_PAGE
            else None
        )

        closed = 0
        for row in await _tracked_loops():
            if row["external_id"] in active:
                continue
            if watermark and row["occurred_at"] < watermark:
                continue
            await repo_agenda.close_open_loop(row["id"])
            closed += 1
        return closed


# --- parsing -----------------------------------------------------------------


def _poll_interval(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["x-poll-interval"])
    except (KeyError, ValueError):
        return None


def _parsed(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_item(thread: dict) -> Item:
    subject = thread.get("subject") or {}
    repository = thread.get("repository") or {}
    subject_url = subject.get("url")
    return Item(
        external_id=str(thread.get("id", "")),
        version=str(thread.get("updated_at", "")),
        kind="notification",
        title=str(subject.get("title") or ""),
        actor=str(repository.get("full_name") or ""),
        url=_html_url(subject_url),
        occurred_at=_parsed(thread.get("updated_at")),
        payload={
            "reason": thread.get("reason"),
            "subject_type": subject.get("type"),
            "subject_url": subject_url,
            "repo": repository.get("full_name"),
            "html_url": _html_url(subject_url),
            "thread_url": thread.get("url"),
        },
    )


def _detail(item: Item) -> str:
    """Where their words live. Nothing renders this to the model — `open_loops_list` and
    `agent loops list` both show the title only, which is the point; `agent loops show` is
    how a human reads it."""
    parts = [item.payload.get("html_url") or "", f"reason: {item.payload.get('reason')}"]
    if item.title:
        parts.append(f"subject: {item.title[:500]}")
    return "\n".join(p for p in parts if p)


async def _tracked_loops() -> list[dict]:
    return await tracked_loops("github")
