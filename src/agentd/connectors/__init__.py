"""Connectors: the daemon's eyes on things outside this machine.

This module is the one place a connector is registered. There is no plugin discovery and no
`agent connectors add`, because a connector is code that holds a credential — adding one
should be a diff somebody reads, not a row somebody inserts.
"""

from __future__ import annotations

from ..config import Config
from .base import Connector

__all__ = ["all_connectors", "Connector"]


def all_connectors(cfg: Config) -> list[Connector]:
    """Every connector the config declares — configured or not.

    A declared-but-unconfigured connector still gets a task: it records why it is parked and
    waits, so `agent doctor` and `agent connectors list` can tell you it is waiting on a
    token rather than silently omitting it. Only the master switch removes it entirely.
    """
    if not cfg.connectors.enabled:
        return []

    out: list[Connector] = []
    if cfg.connectors.github.enabled:
        from .github import GithubConnector

        out.append(GithubConnector(cfg))
    return out
