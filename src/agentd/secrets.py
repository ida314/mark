"""Credentials for the connectors, in the one place the agent is fenced out of.

`~/.config/agent/secrets.toml` has been a hard-denied path in the policy since pass 1
(`config/policy.default.yaml`), waiting for something to put there. This is that something.

**What this actually protects, stated honestly.** It is a 0600 file read unattended by a
daemon that starts at boot, so it is access control, not encryption. It defends against:

- the *agent* reading credentials with its own tools — `~/.config` sits outside
  `paths.allowed_roots`, so `fs-outside-roots` denies it, and `secrets-paths` denies it again
  independently;
- the sandboxed shell, which mounts only the workspace and so has no path to `~/.config` at
  all — the strongest of the three, since it is a property of the container, not a rule;
- leakage into logs, spans, audit rows and notifications, via `Secret` and `redact`;
- other users on the box.

It does **not** defend against anything running as this user, or root, or an offline copy of
the home directory. A key stored beside the thing it encrypts would be theatre, so there is
none. If that constraint ever needs to be real, the answer is a separate uid for the daemon
and a broker socket — not a cipher.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

SECRETS_FILE = CONFIG_DIR / "secrets.toml"


class VaultPermissionError(Exception):
    """The vault is readable by someone other than its owner."""


class Secret:
    """A string that will not wander into a log line by accident.

    `repr`, `str` and `format` all mask, so an f-string in an error path cannot leak it.
    Getting the real value takes saying `reveal()`, which is easy to grep for in review.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __format__(self, _spec: str) -> str:
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def fingerprint(self) -> str:
        """Enough to tell two credentials apart in a listing, not enough to use one."""
        return hashlib.sha256(self._value.encode()).hexdigest()[:8]


def check_permissions(path: Path | None = None) -> str | None:
    """Returns a human-readable problem, or None. Same rule ssh applies to a private key."""
    path = path or SECRETS_FILE
    if not path.exists():
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} is mode {stat.S_IMODE(mode):04o}; run: chmod 600 {path}"
    return None


@lru_cache(maxsize=1)
def _load_cached(path_str: str, mtime: float) -> dict[str, Any]:
    return tomllib.loads(Path(path_str).read_text())


def load(path: Path | None = None) -> dict[str, Any]:
    """Parse the vault. Cached on mtime so a rewrite is picked up without a restart."""
    path = path or SECRETS_FILE
    if not path.exists():
        return {}
    problem = check_permissions(path)
    if problem:
        raise VaultPermissionError(problem)
    return _load_cached(str(path), path.stat().st_mtime)


def _split(ref: str) -> list[str]:
    return [part for part in ref.split("/") if part]


def get(ref: str, field: str, path: Path | None = None) -> Secret | None:
    """`get("google/dylan@nyu.edu", "refresh_token")`. Missing is None, never an exception —
    a connector that is not configured should report itself, not crash the daemon."""
    node: Any = load(path)
    for part in _split(ref):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict):
        return None
    value = node.get(field)
    return Secret(str(value)) if isinstance(value, str | int | float) and value != "" else None


def describe(path: Path | None = None) -> list[tuple[str, list[str]]]:
    """(ref, field names) for every entry. Never values — this is what `agent secrets list`
    prints, and a listing that shows values is a listing you cannot use over someone's
    shoulder."""
    out: list[tuple[str, list[str]]] = []

    def walk(node: dict[str, Any], prefix: str) -> None:
        fields = [k for k, v in node.items() if not isinstance(v, dict)]
        if fields and prefix:
            out.append((prefix, sorted(fields)))
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, f"{prefix}/{key}" if prefix else key)

    data = load(path)
    if isinstance(data, dict):
        walk(data, "")
    return sorted(out)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render(data: dict[str, Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    if scalars and prefix:
        lines.append(f"[{prefix}]")
        for key, value in scalars.items():
            if isinstance(value, list):
                lines.append(f"{key} = [" + ", ".join(_quote(str(v)) for v in value) + "]")
            elif isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            elif isinstance(value, int | float):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f"{key} = {_quote(str(value))}")
        lines.append("")
    for key, value in data.items():
        if isinstance(value, dict):
            quoted = key if key.replace("-", "_").replace(".", "_").isidentifier() else _quote(key)
            lines.extend(_render(value, f"{prefix}.{quoted}" if prefix else quoted))
    return lines


def put(ref: str, path: Path | None = None, **fields: Any) -> None:
    """Merge fields into one entry and rewrite the file atomically at 0600."""
    path = path or SECRETS_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = dict(load(path)) if path.exists() else {}

    node = data
    for part in _split(ref):
        child = node.get(part)
        node[part] = child = dict(child) if isinstance(child, dict) else {}
        node = child
    node.update({k: (v.reveal() if isinstance(v, Secret) else v) for k, v in fields.items()})

    body = "\n".join(
        ["# Written by `agent secrets`. Mode 0600; the policy hard-denies this path.", ""]
        + _render(data)
    ).rstrip() + "\n"

    # Same directory, so os.replace is atomic, and 0600 before any content is written.
    tmp = path.with_suffix(".toml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    _load_cached.cache_clear()


def remove(ref: str, path: Path | None = None) -> bool:
    path = path or SECRETS_FILE
    data = dict(load(path)) if path.exists() else {}
    parts = _split(ref)
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return False
        node[part] = child = dict(child)
        node = child
    if not parts or parts[-1] not in node:
        return False
    node.pop(parts[-1])
    body = "\n".join(
        ["# Written by `agent secrets`. Mode 0600; the policy hard-denies this path.", ""]
        + _render(data)
    ).rstrip() + "\n"
    tmp = path.with_suffix(".toml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _load_cached.cache_clear()
    return True
