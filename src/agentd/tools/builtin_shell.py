"""Sandboxed shell. Every command runs in a throwaway, hardened container.

The docker socket is never mounted: on this host the docker group is effectively root.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..config import get_config
from ..ids import uuid7
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import UNSAFE_WRITE


def mount_source() -> Path:
    """The host directory the container sees at /workspace.

    `paths.project` when one is configured, so a command can actually read and build the
    project it was delegated against, and the workspace otherwise. Never both: /workspace is
    one path, and a worker told to run the suite needs it to be the project's.
    """
    cfg = get_config()
    return cfg.paths.project_root or cfg.paths.workspace


def docker_command(command: str, *, network: bool, timeout_s: int, name: str) -> list[str]:
    cfg = get_config()
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "bridge" if network else "none",
        # 512m, not the 256m this was: the sandbox image runs its own Postgres with
        # PGDATA under this tmpfs (the root filesystem is read-only), and pytest puts
        # its tmp_path roots here too. One suite run peaks at 188MB. At 256m a second
        # run in the same container fills the tmpfs and reports 18 failed / 301 errors
        # - a wrong answer that looks like a real one, which is the worst outcome for
        # a worker whose job is to iterate on the suite.
        "--read-only", "--tmpfs", "/tmp:rw,size=512m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", cfg.sandbox.memory, "--cpus", cfg.sandbox.cpus,
        "--user", "1000:1000",
        # rw, and the project when there is one: a worker whose job is to change code and
        # run the suite cannot do either through a read-only mount of an empty directory.
        "-v", f"{mount_source()}:/workspace:rw",
        "-w", "/workspace",
        cfg.sandbox.image,
        "bash", "-lc", command,
    ]


def _preview(args: dict) -> str:
    cfg = get_config()
    net = "network ON" if args.get("network") else "network off"
    source = mount_source()
    what = "project" if cfg.paths.project_root is not None else "workspace"
    return (
        f"$ {args['command']}\n\n"
        f"in container {cfg.sandbox.image}, {net}, read-only root, "
        f"{what} {source} mounted read-write at /workspace"
    )


@tool(
    "shell_exec",
    "Run a shell command inside an isolated container. The configured project directory is "
    "mounted read-write at /workspace, which is the working directory; changes to it persist "
    "on the host. With no project configured, the agent workspace is mounted there instead.",
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
    # unsafe_write: the argument is a command line the model wrote, so there is nothing
    # generic to reason about. The container is throwaway but `/workspace` is mounted rw
    # and survives it - and it is now the real project directory, so a command here edits
    # the same working tree the user does - and with `network=true` the command can reach
    # anything the host can. No class short of this one is defensible for an arbitrary
    # program.
    effect_class=UNSAFE_WRITE,
)
async def shell_exec(args: dict, ctx: ToolContext) -> ToolResult:
    cfg = get_config()
    if not shutil.which("docker"):
        return ToolResult(content="Docker is not available, so the sandbox is disabled.", ok=False)
    source = mount_source()
    if not source.is_dir():
        # Said, not worked around. `docker run -v` would create a missing host directory as
        # root and hand back a container that looks fine and can see nothing, and silently
        # mounting the workspace instead would be the same lie with a plausible directory
        # in it: every command would run against the wrong tree and report success.
        return ToolResult(
            content=f"The directory to mount at /workspace does not exist: {source}",
            ok=False,
        )
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
