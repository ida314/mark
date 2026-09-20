"""Sandboxed shell. Every command runs in a throwaway, hardened container.

The docker socket is never mounted: on this host the docker group is effectively root.
"""

from __future__ import annotations

import asyncio
import shutil

from ..config import get_config
from ..ids import uuid7
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import UNAUDITED


def docker_command(command: str, *, network: bool, timeout_s: int, name: str) -> list[str]:
    cfg = get_config()
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "bridge" if network else "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=256m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", cfg.sandbox.memory, "--cpus", cfg.sandbox.cpus,
        "--user", "1000:1000",
        "-v", f"{cfg.paths.workspace}:/workspace:rw",
        "-w", "/workspace",
        cfg.sandbox.image,
        "bash", "-lc", command,
    ]


def _preview(args: dict) -> str:
    cfg = get_config()
    net = "network ON" if args.get("network") else "network off"
    return (
        f"$ {args['command']}\n\n"
        f"in container {cfg.sandbox.image}, {net}, read-only root, "
        f"workspace {cfg.paths.workspace} mounted at /workspace"
    )


@tool(
    "shell_exec",
    "Run a shell command inside an isolated container with the workspace mounted at /workspace.",
    required(
        obj(
            command={"type": "string"},
            timeout_s={"type": "integer", "minimum": 1, "maximum": 600},
            network={
                "type": "boolean",
                "description": "Allow network access (higher risk, needs approval)",
            },
        ),
        "command",
    ),
    risk="write",
    tags=("sandbox", "shell", "egress"),
    preview=_preview,
    effect_class=UNAUDITED,
)
async def shell_exec(args: dict, ctx: ToolContext) -> ToolResult:
    cfg = get_config()
    if not shutil.which("docker"):
        return ToolResult(content="Docker is not available, so the sandbox is disabled.", ok=False)
    timeout = min(int(args.get("timeout_s", cfg.sandbox.default_timeout_s)), cfg.sandbox.max_timeout_s)
    network = bool(args.get("network", False))
    name = f"agent-sbx-{uuid7().hex[:12]}"
    cmd = docker_command(args["command"], network=network, timeout_s=timeout, name=name)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 10)
    except TimeoutError:
        await (await asyncio.create_subprocess_exec("docker", "kill", name)).wait()
        proc.kill()
        return ToolResult(content=f"Command timed out after {timeout}s and was killed.", ok=False)

    text = out.decode(errors="replace")
    ok = proc.returncode == 0
    if len(text) > 16000:
        text = text[:16000] + f"\n... [truncated, exit={proc.returncode}]"
    return ToolResult(
        content=f"exit={proc.returncode}\n{text or '(no output)'}",
        ok=ok,
        # Output is produced by code the model wrote: treat it as data, not instructions.
        trust="untrusted",
        data={"exit_code": proc.returncode},
    )


# The `network=true` variant is a stricter risk level; the policy sees the argument.
shell_exec.risk = "write"

TOOLS: list[Tool] = [shell_exec]
