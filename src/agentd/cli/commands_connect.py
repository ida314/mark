"""Bodies for `agent connectors ...`.

Separate from `app.py` because that file is long enough, and because everything here takes
`cfg` as a parameter rather than reaching for `get_config()` — which keeps the module out of
the monkeypatch list in `tests/conftest.py` and makes each of these callable from a test.
"""

from __future__ import annotations

import httpx
from rich.console import Console
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
            status, detail = "[yellow]not set up[/yellow]", why
        elif not state.get("enabled", True):
            status = "[red]disabled[/red]"
            detail = state.get("disabled_reason") or "disabled by hand"
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
        console.print(f"[yellow]{name} is not set up:[/yellow] {why}")
        return 1

    async with httpx.AsyncClient(
        follow_redirects=False, timeout=cfg.connectors.timeout_s
    ) as client:
        result = await poll_once(connector, cfg, client)

    state = await repo_connectors.load_state(name)
    if result is None:
        console.print(f"[red]{name}: {state.get('last_error') or 'disabled'}[/red]")
        if state.get("disabled_reason"):
            console.print(f"[dim]{state['disabled_reason']}[/dim]")
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
            row(f"connector:{connector.name}", None, why)
        elif not state.get("enabled", True):
            row(f"connector:{connector.name}", False, state.get("disabled_reason") or "disabled")
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
