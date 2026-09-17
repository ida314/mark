"""Git-backed markdown memory: the human-readable, diffable, revertible layer.

Generated blocks are owned by the agent and regenerated from Postgres. Everything
outside them belongs to the user and is never overwritten.
"""

from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path

BEGIN = "<!-- agent:generated:begin -->"
END = "<!-- agent:generated:end -->"
AGENT_AUTHOR = "agent-consolidator <agent@localhost>"
USER_AUTHOR = "user <user@localhost>"

SEED_FILES = {
    "README.md": """# Memory

This directory is the agent's long-term memory in human-readable form. It is a git
repository, so every change the agent makes is a commit you can read, diff and revert.

- Text **outside** the `agent:generated` markers is yours. The agent never rewrites it.
- Text **inside** the markers is generated from the database and will be regenerated.
- `agent/instructions.md` is yours alone: the agent reads it every turn and never writes it.
""",
    "agent/instructions.md": """# Standing instructions

Anything you write here is included in the agent's system prompt on every turn.
Keep it short and concrete.

-
""",
    "profile/core.md": "# Core profile\n\nWho I am, in my own words:\n\n",
    "profile/preferences.md": "# Preferences\n\nHow I like to be worked with:\n\n",
}


class MarkdownRepo:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- git plumbing --------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    @contextmanager
    def _lock(self):
        """One writer at a time: the daemon and the CLI share this repo."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".agent.lock"
        with lock_path.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def is_repo(self) -> bool:
        return (self.root / ".git").is_dir()

    def init(self) -> None:
        with self._lock():
            if not self.is_repo():
                self._git("init", "-q")
                self._git("config", "user.email", "agent@localhost")
                self._git("config", "user.name", "agent")
            for rel, body in SEED_FILES.items():
                path = self.root / rel
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(body)
            gitignore = self.root / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(".agent.lock\n")
            self._commit_all("Initialize memory repository", AGENT_AUTHOR)

    def _commit_all(self, message: str, author: str) -> str | None:
        self._git("add", "-A")
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            return None
        self._git("commit", "-q", "-m", message, f"--author={author}")
        return self._git("rev-parse", "HEAD").stdout.strip()

    def commit(self, message: str, author: str = AGENT_AUTHOR) -> str | None:
        with self._lock():
            return self._commit_all(message, author)

    def log(self, limit: int = 20) -> list[dict]:
        if not self.is_repo():
            return []
        out = self._git(
            "log", f"-{limit}", "--pretty=format:%h\x1f%an\x1f%ad\x1f%s", "--date=short",
            check=False,
        ).stdout
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                rows.append(
                    {"sha": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
                )
        return rows

    def revert(self, sha: str) -> str:
        with self._lock():
            result = self._git("revert", "--no-edit", sha, check=False)
            return result.stdout + result.stderr

    def has_uncommitted_changes(self) -> bool:
        return bool(self._git("status", "--porcelain", check=False).stdout.strip())

    def diff(self) -> str:
        return self._git("diff", check=False).stdout

    # --- content -------------------------------------------------------------

    def read_core(self) -> str:
        parts = []
        for rel in ("profile/core.md", "profile/preferences.md"):
            path = self.root / rel
            if path.is_file():
                parts.append(f"## {rel}\n{path.read_text().strip()}")
        return "\n\n".join(parts)

    def read_section(self, section: str) -> str:
        """Accept 'goals', 'profile/core', or a full relative path."""
        candidates = [
            self.root / section,
            self.root / f"{section}.md",
            self.root / section / "index.md",
        ]
        for path in candidates:
            if path.is_file():
                return path.read_text()
        directory = self.root / section
        if directory.is_dir():
            return "\n\n".join(
                f"## {p.relative_to(self.root)}\n{p.read_text().strip()}"
                for p in sorted(directory.glob("*.md"))
            )
        return ""

    def write_generated(self, rel_path: str, generated: str, *, header: str | None = None) -> bool:
        """Replace only the generated block; leave the user's prose alone. True if changed."""
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        block = f"{BEGIN}\n{generated.strip()}\n{END}"
        note = (
            "<!-- Text above this block is yours; the agent only rewrites the block below. -->"
        )
        if path.exists():
            old = path.read_text()
            if BEGIN in old and END in old:
                head, rest = old.split(BEGIN, 1)
                _, tail = rest.split(END, 1)
                new = f"{head}{block}{tail}"
            else:
                new = f"{old.rstrip()}\n\n{note}\n{block}\n"
        else:
            title = header or rel_path.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ").title()
            new = f"# {title}\n\n{note}\n{block}\n"
        if path.exists() and path.read_text() == new:
            return False
        path.write_text(new)
        return True

    def user_text(self, rel_path: str) -> str:
        """The part of a file the user wrote, with generated blocks stripped out."""
        path = self.root / rel_path
        if not path.is_file():
            return ""
        text = path.read_text()
        while BEGIN in text and END in text:
            head, rest = text.split(BEGIN, 1)
            _, tail = rest.split(END, 1)
            text = head + tail
        return text.strip()
