"""The other half of the MCP seam: tools from *other* people's servers, imported as our own.

Everything imported here is foreign code on the far side of a pipe, so it arrives with
`source="mcp:<server>"` (which the shipped policy sends to approval), `trust_output=False`
(so the executor wraps results in `<untrusted_content>`) and whatever risk the server is
configured for. A server that lies about its annotations can therefore only ever make its
own tools *look* safer in the listing — it cannot lower the gate it has to pass.

Each server runs in a task that owns its connection start to finish. That is deliberate:
the SDK's transports are anyio context managers whose cancel scopes belong to the task that
entered them, so closing one from another task is how you get "Attempted to exit cancel
scope in a different task".
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .config import Config, McpServerConfig, get_config
from .obs import otel
from .tools.base import Tool, ToolContext, ToolResult
from .tools.effects import UNSAFE_WRITE
from .tools.registry import Registry, get_registry

_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


def qualified_name(server: str, tool: str) -> str:
    """`mcp_notes_search`: namespaced so two servers can both have a `search`."""
    return _NAME_OK.sub("_", f"mcp_{server}_{tool}")[:64]


def _risk_for(spec: McpServerConfig, tool: types.Tool) -> str:
    """The server's own hints, but only when the user has said to believe them."""
    if not spec.trust_annotations or tool.annotations is None:
        return spec.risk_default
    if tool.annotations.destructive_hint:
        return "destructive"
    if tool.annotations.read_only_hint:
        return "read"
    return spec.risk_default


def _render(result: types.CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:  # images, audio, embedded resources: say what arrived, not the bytes
            parts.append(f"[{getattr(block, 'type', 'content')}]")
    if not parts and result.structured_content is not None:
        parts.append(str(result.structured_content))
    return "\n".join(parts).strip() or "(no output)"


class ServerConnection:
    """One external server: a task, a session, and a way to stop it cleanly."""

    def __init__(self, name: str, spec: McpServerConfig) -> None:
        self.name = name
        self.spec = spec
        self.session: ClientSession | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    async def _run(self) -> None:
        try:
            if self.spec.url:
                from mcp.client.streamable_http import streamable_http_client

                async with streamable_http_client(self.spec.url) as (read, write, _):
                    await self._serve(read, write)
            else:
                params = StdioServerParameters(
                    command=self.spec.command or "",
                    args=list(self.spec.args),
                    env={**self.spec.env} or None,
                )
                async with stdio_client(params) as (read, write):
                    await self._serve(read, write)
        except Exception as exc:  # a broken server must not take the agent down
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.session = None
            self._ready.set()

    async def _serve(self, read, write) -> None:
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=self.spec.timeout_s)
            self.session = session
            self._ready.set()
            await self._stop.wait()

    async def start(self) -> bool:
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.spec.timeout_s + 5)
        except TimeoutError:
            self.error = "timed out connecting"
        return self.session is not None

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def list_tools(self) -> list[types.Tool]:
        if self.session is None:
            return []
        result = await self.session.list_tools()
        tools = list(result.tools)
        if self.spec.allow_tools is not None:
            allowed = set(self.spec.allow_tools)
            tools = [t for t in tools if t.name in allowed]
        return tools

    def wrap(self, remote: types.Tool) -> Tool:
        spec = self.spec

        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            if self.session is None:
                return ToolResult(
                    content=f"MCP server '{self.name}' is not connected: {self.error or 'stopped'}",
                    ok=False,
                )
            with otel.span(
                "mcp.call", {"mcp.server": self.name, "mcp.tool": remote.name}
            ):
                result = await self.session.call_tool(
                    remote.name, args, read_timeout_seconds=spec.timeout_s
                )
            return ToolResult(
                content=_render(result),
                ok=not result.is_error,
                trust="trusted" if spec.trust_output else "untrusted",
                data={"server": self.name, "remote_tool": remote.name},
            )

        schema = remote.input_schema or {"type": "object", "properties": {}}
        description = (remote.description or remote.title or remote.name).strip()
        return Tool(
            name=qualified_name(self.name, remote.name),
            description=f"[{self.name}] {description}"[:1000],
            parameters=schema,
            handler=handler,
            # A remote tool cannot be classified from here: the protocol has no effect
            # class, `read_only_hint` is the server's own claim about itself, and this
            # process did not write the thing on the other end. `unsafe_write` is the
            # answer that cannot duplicate an action on resume. A per-server declaration
            # in config would be the way to say otherwise, and nobody needs one yet.
            effect_class=UNSAFE_WRITE,
            risk=_risk_for(spec, remote),
            tags=("mcp", self.name),
            source=f"mcp:{self.name}",
            trust_output=spec.trust_output,
        )


class McpClients:
    """All configured servers. One instance per process; `close()` is the whole teardown."""

    def __init__(self) -> None:
        self.connections: dict[str, ServerConnection] = {}
        self.loaded: dict[str, list[str]] = {}
        self.failures: dict[str, str] = {}
        self.warnings: list[str] = []

    async def load(self, cfg: Config | None = None, registry: Registry | None = None) -> int:
        cfg = cfg or get_config()
        registry = registry or get_registry()
        added = 0
        for name, spec in cfg.mcp.servers.items():
            if not spec.enabled or name in self.connections:
                continue
            if not spec.command and not spec.url:
                self.failures[name] = "neither command nor url configured"
                continue
            conn = ServerConnection(name, spec)
            if not await conn.start():
                self.failures[name] = conn.error or "failed to connect"
                await conn.stop()
                continue
            self.connections[name] = conn
            try:
                remotes = await conn.list_tools()
            except Exception as exc:
                self.failures[name] = f"list_tools failed: {exc}"
                continue
            tools = [conn.wrap(r) for r in remotes]
            registry.add(*tools)
            self.loaded[name] = [t.name for t in tools]
            added += len(tools)
        if added:
            # Tool selection ranks by embedding, so an unsynced tool is an invisible one.
            try:
                await registry.sync_embeddings()
            except Exception as exc:
                self.warnings.append(f"tool embeddings not synced: {exc}")
        return added

    async def close(self) -> None:
        for conn in list(self.connections.values()):
            await conn.stop()
        self.connections.clear()


_clients: McpClients | None = None


async def load_external_tools(
    cfg: Config | None = None, registry: Registry | None = None
) -> McpClients:
    """Idempotent: safe to call from chat, `ask`, and the daemon alike."""
    global _clients
    if _clients is None:
        _clients = McpClients()
    await _clients.load(cfg, registry)
    return _clients


async def close_external_tools() -> None:
    global _clients
    if _clients is not None:
        await _clients.close()
        _clients = None
