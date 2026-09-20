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

    # One connector per account per service, not one per service. Gmail and Calendar share a
    # credential but not a failure: Gmail being rate-limited at midday should not stop the
    # calendar poll, and `agent connectors list` should say which of the two is unhappy.
    if cfg.connectors.google.enabled:
        from .gcal import GoogleCalendarConnector
        from .gmail import GmailConnector

        for label, account in sorted(cfg.connectors.google.accounts.items()):
            if account.mail:
                out.append(GmailConnector(cfg, label))
            if account.calendar:
                out.append(GoogleCalendarConnector(cfg, label))

    if cfg.connectors.imap.enabled:
        from .imap_mail import ImapConnector

        out.extend(ImapConnector(cfg, label) for label in sorted(cfg.connectors.imap.accounts))

    if cfg.connectors.brightspace.enabled:
        from .brightspace import BrightspaceConnector

        out.append(BrightspaceConnector(cfg))
    return out
