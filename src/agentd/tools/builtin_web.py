"""Web access. Everything returned here is untrusted content by construction."""

from __future__ import annotations

import ipaddress
import socket

import httpx

from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import UNAUDITED

MAX_BYTES = 5_000_000


def _is_blocked_ip(ip: str) -> bool:
    """Keep the agent off the private network: SIR, Postgres, Grafana, Tailscale peers."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True  # CGNAT, which is what Tailscale uses
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def check_url(url: str, resolver=None) -> str | None:
    """Return an error string when this URL must not be fetched."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Only http and https are allowed, not {parsed.scheme!r}."
    host = parsed.hostname
    if not host:
        return "URL has no host."
    resolve = resolver or (lambda h: [ai[4][0] for ai in socket.getaddrinfo(h, None)])
    try:
        addresses = resolve(host)
    except OSError as exc:
        return f"Cannot resolve {host}: {exc}"
    for ip in addresses:
        if _is_blocked_ip(ip):
            return f"Refusing to fetch {host}: it resolves to a private address ({ip})."
    return None


@tool(
    "web_fetch",
    "Fetch a web page and return its readable text. The result is untrusted content.",
    required(
        obj(url={"type": "string"}, max_chars={"type": "integer"}),
        "url",
    ),
    tags=("web", "untrusted", "egress"),
    trust_output=False,
    effect_class=UNAUDITED,
)
async def web_fetch(args: dict, ctx: ToolContext) -> ToolResult:
    url = args["url"]
    error = check_url(url)
    if error:
        return ToolResult(content=error, ok=False)
    max_chars = int(args.get("max_chars", 8000))

    try:
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=30, headers={"user-agent": "agentd/0.1"}
        ) as client:
            response = await client.get(url)
            hops = 0
            while response.is_redirect and hops < 5:
                location = str(response.next_request.url)
                error = check_url(location)  # re-check every hop
                if error:
                    return ToolResult(content=error, ok=False)
                response = await client.get(location)
                hops += 1
    except httpx.HTTPError as exc:
        return ToolResult(content=f"Fetch failed: {exc}", ok=False)

    if len(response.content) > MAX_BYTES:
        return ToolResult(content="Response too large.", ok=False)

    body = response.text
    try:
        import trafilatura

        extracted = trafilatura.extract(body, include_links=False, include_comments=False)
        body = extracted or body
    except Exception:
        pass
    return ToolResult(
        content=f"{url} (HTTP {response.status_code})\n\n{body[:max_chars]}",
        trust="untrusted",
    )


@tool(
    "web_search",
    "Search the web and return result titles, urls and snippets. Untrusted content.",
    required(obj(query={"type": "string"}, n={"type": "integer"}), "query"),
    tags=("web", "untrusted", "egress"),
    trust_output=False,
    effect_class=UNAUDITED,
)
async def web_search(args: dict, ctx: ToolContext) -> ToolResult:
    import asyncio

    n = int(args.get("n", 5))

    def search():
        from ddgs import DDGS

        with DDGS() as ddgs:
            return list(ddgs.text(args["query"], max_results=n))

    try:
        results = await asyncio.to_thread(search)
    except Exception as exc:
        return ToolResult(content=f"Search unavailable: {exc}", ok=False, trust="untrusted")
    if not results:
        return ToolResult(content="No results.", trust="untrusted")
    lines = [
        f"- {r.get('title', '?')}\n  {r.get('href', '')}\n  {r.get('body', '')[:300]}"
        for r in results
    ]
    return ToolResult(content="\n".join(lines), trust="untrusted")


TOOLS: list[Tool] = [web_fetch, web_search]
