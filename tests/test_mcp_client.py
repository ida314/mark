"""Importing tools from someone else's MCP server, over a real stdio pipe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentd.config import McpServerConfig
from agentd.mcp_client import McpClients, qualified_name
from agentd.policy.approvals import AutoApprover
from agentd.policy.engine import engine_from_config
from agentd.tools.base import ToolContext
from agentd.tools.executor import ToolExecutor
from agentd.tools.registry import Registry

FIXTURE = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"

pytestmark = pytest.mark.slow


def _spec(**overrides) -> McpServerConfig:
    return McpServerConfig(
        command=sys.executable, args=[str(FIXTURE)], timeout_s=20.0, **overrides
    )


async def _load(cfg, registry: Registry, **overrides) -> McpClients:
    cfg = cfg.model_copy(deep=True)
    cfg.mcp.servers = {"echo": _spec(**overrides)}
    clients = McpClients()
    await clients.load(cfg, registry)
    return clients


async def test_tools_are_imported_namespaced_and_marked_untrusted(cfg):
    registry = Registry()
    clients = await _load(cfg, registry)
    try:
        assert not clients.failures, clients.failures
        name = qualified_name("echo", "echo")
        tool = registry.get(name)
        assert tool is not None
        assert tool.source == "mcp:echo"
        assert tool.trust_output is False
        # no annotations are believed by default, so everything lands on the configured risk
        assert tool.risk == "external"
        assert registry.get(qualified_name("echo", "peek")).risk == "external"
    finally:
        await clients.close()


async def test_a_read_only_hint_counts_only_when_the_server_is_trusted_to_say_so(cfg):
    registry = Registry()
    clients = await _load(cfg, registry, trust_annotations=True)
    try:
        assert registry.get(qualified_name("echo", "peek")).risk == "read"
        assert registry.get(qualified_name("echo", "echo")).risk == "external"
        # The hint moves `risk`, which asks whether to prompt the user first. It does not
        # move `effect_class`, which asks whether a crash may re-run the call: a server
        # describing itself is not in a position to promise that, so a tool that arrived
        # over a pipe stays at the class that cannot duplicate an action.
        for remote in ("peek", "echo"):
            assert registry.get(qualified_name("echo", remote)).effect_class == "unsafe_write"
    finally:
        await clients.close()


async def test_calling_through_the_executor_wraps_the_output_as_untrusted(cfg):
    registry = Registry()
    clients = await _load(cfg, registry)
    try:
        executor = ToolExecutor(registry.tools, engine_from_config(cfg), AutoApprover(True))
        result = await executor.run(
            qualified_name("echo", "echo"),
            {"text": "hello", "reason": "testing the pipe"},
            ToolContext(autonomy="act"),
        )
        assert result.ok
        assert "echo: hello" in result.content
        assert "<untrusted_content" in result.content
    finally:
        await clients.close()


async def test_allow_tools_keeps_the_rest_out(cfg):
    registry = Registry()
    clients = await _load(cfg, registry, allow_tools=["echo"])
    try:
        assert registry.get(qualified_name("echo", "echo")) is not None
        assert registry.get(qualified_name("echo", "peek")) is None
    finally:
        await clients.close()


async def test_imported_tools_need_approval_even_at_the_highest_autonomy(cfg):
    from agentd.policy.engine import PolicyContext, ToolCallInfo

    engine = engine_from_config(cfg)
    call = ToolCallInfo(name=qualified_name("echo", "peek"), risk="read", source="mcp:echo")
    for autonomy in ("assist", "act"):
        decision = engine.evaluate(call, PolicyContext(autonomy=autonomy))
        assert decision.outcome == "require_approval"
        assert decision.rule_id == "mcp-imported-default"


async def test_a_server_that_will_not_start_is_reported_not_raised(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.mcp.servers = {"broken": McpServerConfig(command="definitely-not-a-real-binary", timeout_s=5.0)}
    clients = McpClients()
    registry = Registry()
    await clients.load(cfg, registry)
    try:
        assert "broken" in clients.failures
        assert registry.names() == []
    finally:
        await clients.close()
