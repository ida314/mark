"""The run journal: one append-only event log per run.

`state = fold(reduce, journal, initial)`. Session 2a builds the store and the writer only -
nothing in the runtime emits events yet (2b) and nothing reads them for recovery (Pass 4).
"""

from .store import (
    SCHEMA_VERSION,
    Event,
    JournalError,
    JournalStore,
    PendingEvent,
    PruneResult,
    Retention,
    RunInfo,
    SchemaTooNew,
    SeqConflict,
    default_path,
    retention_from_config,
)
from .writer import SYNC_PREFIXES, SYNC_TYPES, JournalWriter, is_synchronous

__all__ = [
    "SCHEMA_VERSION",
    "SYNC_PREFIXES",
    "SYNC_TYPES",
    "Event",
    "JournalError",
    "JournalStore",
    "JournalWriter",
    "PendingEvent",
    "PruneResult",
    "Retention",
    "RunInfo",
    "SchemaTooNew",
    "SeqConflict",
    "default_path",
    "is_synchronous",
    "retention_from_config",
]
