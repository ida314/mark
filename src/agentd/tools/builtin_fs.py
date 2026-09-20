"""Filesystem tools. Policy confines them to the allowed roots; these stay simple."""

from __future__ import annotations

import asyncio
import difflib
import shutil
from pathlib import Path

from ..config import get_config
from ..ids import utcnow
from .base import Tool, ToolContext, ToolResult, obj, required, tool
from .effects import UNAUDITED

MAX_READ_BYTES = 400_000


def _p(path: str) -> Path:
    """Relative paths mean the agent's workspace, never whatever directory it was started in."""
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        return get_config().paths.workspace / expanded
    return expanded


@tool(
    "fs_list",
    "List the entries of a directory.",
    required(obj(path={"type": "string", "description": "Directory to list"}), "path"),
    tags=("fs",),
    path_args=("path",),
    effect_class=UNAUDITED,
)
async def fs_list(args: dict, ctx: ToolContext) -> ToolResult:
    path = _p(args["path"])
    if not path.is_dir():
        return ToolResult(content=f"Not a directory: {path}", ok=False)
    lines = []
    for entry in sorted(path.iterdir())[:500]:
        marker = "/" if entry.is_dir() else ""
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        lines.append(f"{entry.name}{marker}\t{size}")
    return ToolResult(content="\n".join(lines) or "(empty directory)")


@tool(
    "fs_read",
    "Read a text file, optionally a line range.",
    required(
        obj(
            path={"type": "string"},
            offset={"type": "integer", "description": "First line (1-based)"},
            limit={"type": "integer", "description": "Maximum lines to return"},
        ),
        "path",
    ),
    tags=("fs",),
    path_args=("path",),
    effect_class=UNAUDITED,
)
async def fs_read(args: dict, ctx: ToolContext) -> ToolResult:
    path = _p(args["path"])
    if not path.is_file():
        return ToolResult(content=f"Not a file: {path}", ok=False)
    if path.stat().st_size > MAX_READ_BYTES:
        return ToolResult(content=f"File too large ({path.stat().st_size} bytes)", ok=False)
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return ToolResult(content=f"Cannot read {path}: {exc}", ok=False)
    offset = max(1, int(args.get("offset", 1)))
    limit = int(args.get("limit", 800))
    lines = text.splitlines()[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(f"{i + offset:>5}\t{line}" for i, line in enumerate(lines))
    return ToolResult(content=numbered or "(empty)")


@tool(
    "fs_search",
    "Search file contents with ripgrep and return matching lines.",
    required(
        obj(
            pattern={"type": "string", "description": "Regular expression"},
            path={"type": "string", "description": "Directory or file to search"},
            max_results={"type": "integer"},
        ),
        "pattern",
        "path",
    ),
    tags=("fs",),
    path_args=("path",),
    effect_class=UNAUDITED,
)
async def fs_search(args: dict, ctx: ToolContext) -> ToolResult:
    rg = shutil.which("rg")
    max_results = int(args.get("max_results", 100))
    cmd = (
        [rg, "--line-number", "--no-heading", "--color=never", "-m", str(max_results),
         args["pattern"], str(_p(args["path"]))]
        if rg
        else ["grep", "-rn", "-m", str(max_results), args["pattern"], str(_p(args["path"]))]
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        return ToolResult(content="Search timed out", ok=False)
    text = out.decode(errors="replace").strip()
    if not text:
        return ToolResult(content="No matches." + (f"\n{err.decode()[:400]}" if err else ""))
    return ToolResult(content="\n".join(text.splitlines()[:max_results]))


def _write_preview(args: dict) -> str:
    path = _p(args["path"])
    new = args.get("content", "")
    old = path.read_text(errors="replace") if path.is_file() else ""
    mode = args.get("mode", "overwrite")
    if mode == "append":
        new = old + new
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{path.name}", tofile=f"b/{path.name}", lineterm="", n=2,
    )
    body = "\n".join(list(diff)[:60])
    return body or f"(new file {path}, {len(new)} chars)"


@tool(
    "fs_write",
    "Write a text file. Creates a backup so the change can be undone.",
    required(
        obj(
            path={"type": "string"},
            content={"type": "string"},
            mode={"type": "string", "enum": ["overwrite", "append", "create"]},
        ),
        "path",
        "content",
    ),
    risk="write",
    tags=("fs",),
    path_args=("path",),
    preview=_write_preview,
    effect_class=UNAUDITED,
)
async def fs_write(args: dict, ctx: ToolContext) -> ToolResult:
    cfg = get_config()
    path = _p(args["path"])
    mode = args.get("mode", "overwrite")
    if mode == "create" and path.exists():
        return ToolResult(content=f"Refusing to overwrite existing file: {path}", ok=False)
    path.parent.mkdir(parents=True, exist_ok=True)

    undo = None
    if path.is_file():
        backup_dir = cfg.paths.backups / str(ctx.action_id or utcnow().strftime("%Y%m%dT%H%M%S"))
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / path.name
        shutil.copy2(path, backup)
        undo = {"type": "restore_file", "path": str(path), "backup": str(backup)}
    else:
        undo = {"type": "delete_file", "path": str(path)}

    content = args["content"]
    try:
        if mode == "append":
            with path.open("a") as fh:
                fh.write(content)
        else:
            path.write_text(content)
    except OSError as exc:
        return ToolResult(content=f"Write failed: {exc}", ok=False)
    return ToolResult(
        content=f"Wrote {len(content)} chars to {path} ({mode}).",
        undo=undo,
        data={"path": str(path)},
    )


TOOLS: list[Tool] = [fs_list, fs_read, fs_search, fs_write]
