"""Bodies for `agent connectors ...`.

Separate from `app.py` because that file is long enough, and because everything here takes
`cfg` as a parameter rather than reaching for `get_config()` — which keeps the module out of
the monkeypatch list in `tests/conftest.py` and makes each of these callable from a test.
"""

from __future__ import annotations

import asyncio

import httpx
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .. import secrets as vault
from ..config import Config
from ..connectors import all_connectors
from ..connectors.base import Connector, poll_once
from ..db import repo_connectors
from ..ids import utcnow


def _find(cfg: Config, name: str) -> Connector | None:
    return next((c for c in all_connectors(cfg) if c.name == name), None)


def _ago(stamp) -> str:
    if stamp is None:
        return "never"
    seconds = (utcnow() - stamp).total_seconds()
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"


async def render_list(cfg: Config, console: Console) -> None:
    connectors = all_connectors(cfg)
    if not connectors:
        console.print(
            "[dim]no connectors enabled — set [connectors] enabled = true in config.toml[/dim]"
        )
        return

    states = {s["name"]: s for s in await repo_connectors.list_state()}
    table = Table(show_header=True, header_style="bold")
    for column in ("connector", "status", "last ok", "next poll", "detail"):
        table.add_column(column)

    for connector in connectors:
        state = states.get(connector.name, {})
        why = connector.configured()
        if why:
            # `why` is a shell command containing [section] names, and Rich reads square
            # brackets as markup — unescaped, the part that tells you what to fix vanishes.
            status, detail = "[yellow]not set up[/yellow]", escape(why)
        elif not state.get("enabled", True):
            status = "[red]disabled[/red]"
            detail = escape(state.get("disabled_reason") or "disabled by hand")
        else:
            status = "[green]enabled[/green]"
            detail = (
                f"{state.get('items_seen', 0)} items, {state.get('loops_opened', 0)} loops"
                + (f", {state['consecutive_failures']} failures" if state.get("consecutive_failures") else "")
            )
        table.add_row(
            connector.name,
            status,
            _ago(state.get("last_success_at")),
            _ago(state.get("next_poll_at")).replace(" ago", " from now").replace("never", "-"),
            detail,
        )
    console.print(table)


async def poll_now(cfg: Config, name: str, console: Console) -> int:
    """One real poll in the foreground. The fastest way to find out whether a credential
    actually works, which is why it prints what happened rather than staying quiet."""
    connector = _find(cfg, name)
    if connector is None:
        console.print(f"[red]no connector named {name}[/red] (is it enabled in config.toml?)")
        return 1

    why = connector.configured()
    if why:
        console.print(f"[yellow]{name} is not set up:[/yellow] {escape(why)}")
        return 1

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=cfg.connectors.timeout_s
    ) as client:
        result = await poll_once(connector, cfg, client)

    state = await repo_connectors.load_state(name)
    if result is None:
        console.print(f"[red]{name}: {escape(state.get('last_error') or 'disabled')}[/red]")
        if state.get("disabled_reason"):
            console.print(f"[dim]{escape(state['disabled_reason'])}[/dim]")
        return 1

    console.print(
        f"{name}: fetched {len(result.items)}"
        + (f" · {result.note}" if result.note else "")
        + f" · {state.get('items_seen', 0)} items seen in total"
        + f" · {state.get('loops_opened', 0)} loops opened in total"
    )
    return 0


async def set_enabled(cfg: Config, name: str, enabled: bool, console: Console) -> int:
    if _find(cfg, name) is None:
        console.print(f"[red]no connector named {name}[/red]")
        return 1
    await repo_connectors.load_state(name)
    await repo_connectors.set_enabled(
        name, enabled, reason=None if enabled else "disabled by hand"
    )
    console.print(f"{name} {'enabled' if enabled else 'disabled'}")
    return 0


async def reset(cfg: Config, name: str, console: Console) -> int:
    """Forget the cursor. Harmless because ingestion is idempotent by unique index — the
    re-fetch re-archives nothing."""
    if _find(cfg, name) is None:
        console.print(f"[red]no connector named {name}[/red]")
        return 1
    await repo_connectors.load_state(name)
    await repo_connectors.reset_cursor(name)
    console.print(f"{name}: cursor cleared; the next poll will be a full one")
    return 0


async def doctor_rows(cfg: Config, row) -> None:
    """Called by `agent doctor`. Reports status without ever revealing a value."""
    if not cfg.connectors.enabled:
        return
    states = {s["name"]: s for s in await repo_connectors.list_state()}
    for connector in all_connectors(cfg):
        state = states.get(connector.name, {})
        why = connector.configured()
        if why:
            row(f"connector:{connector.name}", None, escape(why))
        elif not state.get("enabled", True):
            row(
                f"connector:{connector.name}",
                False,
                escape(state.get("disabled_reason") or "disabled"),
            )
        else:
            ref = connector.vault_ref
            token = vault.get(ref, "token") if ref else None
            row(
                f"connector:{connector.name}",
                bool(state.get("last_success_at")),
                f"last ok {_ago(state.get('last_success_at'))}, "
                f"{state.get('items_seen', 0)} items"
                + (f", token {token.fingerprint()}" if token else ""),
            )


async def google_auth(cfg: Config, label: str, console: Console, *, port: int = 0) -> int:
    """The one-time browser round trip that turns a Cloud OAuth client into a refresh token.

    Loopback, not the old out-of-band flow, because Google removed that one. The practical
    consequence on a headless box is that the browser has to reach 127.0.0.1 *on this
    machine*, so `--port 8771` plus `ssh -L 8771:127.0.0.1:8771` is the documented way in
    from a laptop. Not 8765, which this box gave to something else years ago. Google exempts
    loopback redirects from exact URI matching, so a Desktop client accepts whichever port
    you pick without registering it. The port is printed either way, so the failure mode is
    visible rather than a consent page that hangs.

    What lands in the vault is the refresh token and the scopes, never the access token: an
    access token is worth an hour and caching it on disk would be a liability for no gain.
    """
    from ..connectors import google_auth as oauth

    account = cfg.connectors.google.accounts.get(label)
    if account is None:
        known = ", ".join(sorted(cfg.connectors.google.accounts)) or "none configured"
        console.print(f"[red]no Google account labelled {label}[/red] (have: {known})")
        return 1
    if not account.address:
        console.print(f"[red]set [connectors.google.accounts.{label}] address first[/red]")
        return 1
    if vault.get(oauth.CLIENT_REF, "client_id") is None:
        console.print(
            f"[yellow]no OAuth client yet.[/yellow] Create a Desktop client in a Google Cloud "
            f"project with the Gmail and Calendar APIs enabled, then:\n"
            f"  agent secrets set {oauth.CLIENT_REF} client_id\n"
            f"  agent secrets set {oauth.CLIENT_REF} client_secret"
        )
        return 1

    scopes = [oauth.GMAIL_SCOPE] if account.mail else []
    if account.calendar:
        scopes.append(oauth.CALENDAR_SCOPE)
    if not scopes:
        console.print(f"[red]{label} has neither mail nor calendar enabled[/red]")
        return 1

    verifier, challenge = oauth.pkce_pair()
    server, thread = oauth.serve_once(port, timeout_s=300.0)
    bound = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{bound}/"
    client_id = vault.get(oauth.CLIENT_REF, "client_id")
    url = oauth.build_auth_url(cfg, client_id.reveal(), redirect_uri, scopes, challenge,
                               account.address)

    console.print(f"Authorising [bold]{account.address}[/bold] for: {', '.join(scopes)}")
    console.print(f"[dim]listening on 127.0.0.1:{bound} for the redirect[/dim]")
    console.print("\nOpen this in a browser that can reach this machine's loopback:\n")
    console.print(url)
    console.print("\n[dim]waiting (5 minutes)...[/dim]")

    await asyncio.to_thread(thread.join, 300.0)
    server.server_close()
    code, error = oauth._CodeHandler.code, oauth._CodeHandler.error
    oauth._CodeHandler.code = oauth._CodeHandler.error = None
    if error:
        console.print(f"[red]Google refused: {error}[/red]")
        return 1
    if not code:
        console.print("[red]no authorisation code arrived[/red] (the wait ran out)")
        return 1

    async with httpx.AsyncClient(timeout=cfg.connectors.timeout_s) as client:
        try:
            payload = await oauth.exchange_code(
                cfg, client, code=code, verifier=verifier, redirect_uri=redirect_uri
            )
        except Exception as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
    oauth.store(account.address, payload, scopes)

    granted = set(str(payload.get("scope", "")).split())
    missing = [s for s in scopes if s not in granted] if granted else []
    console.print(f"[green]stored[/green] refresh token at {oauth.account_ref(account.address)}")
    if missing:
        # Google shows one checkbox per scope and the user can clear one. Saying so now beats
        # a 403 from the calendar connector an hour later.
        console.print(f"[yellow]not granted:[/yellow] {', '.join(missing)}")
    console.print(f"Now: agent connectors poll gmail-{label}" if account.mail else "")
    return 0
