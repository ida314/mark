"""A throwaway MCP server over stdio, so the client loader is tested against a real pipe."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("echo-fixture")


@mcp.tool()
async def echo(text: str) -> str:
    """Repeat the text back."""
    return f"echo: {text}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def peek() -> str:
    """A tool that claims to be harmless."""
    return "peeked"


if __name__ == "__main__":
    mcp.run(transport="stdio")
