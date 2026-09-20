"""The run journal: one append-only event log per run, and the only event path there is.

`state = fold(reduce, journal, initial)`. Session 2a built the store and the writer; 2b added
the event vocabulary (`events.py`) and the per-run handle the runtime writes through
(`runtime.RunJournal`); 2c added the subscription (`feed.py`), the shared rendering
(`render.py`), and deleted the in-process event bus that used to carry the same facts to the
CLI and to Telegram.

Nothing reads the journal for *recovery* yet - no fold, no checkpoint, no resume. That is
Pass 4.
"""

from .events import (
    CHECKPOINT_TRIGGERS,
    EFFECT_CLASSES,
    EFFECT_STATUSES,
    EMITTED_TYPES,
    EVENT_TYPES,
    EVENTS,
    RUN_STATUSES,
    EventSchemaError,
    Field,
    UnknownEventType,
    preview,
    spec_for,
    step_id,
    validate_payload,
)
from .feed import POLL_INTERVAL_S, JournalTail, turn_ended
from .ledger import (
    EFFECT_STATES,
    Effect,
    EffectLedger,
    EffectRow,
    EffectStateError,
    get_ledger,
    ledgered,
)
from .render import RENDERED_TYPES, Line, brief_args, render_event
from .runtime import RunJournal, close_writer, get_writer
from .store import (
    MIGRATIONS,
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
    "CHECKPOINT_TRIGGERS",
    "EFFECT_CLASSES",
    "EFFECT_STATES",
    "EFFECT_STATUSES",
    "EMITTED_TYPES",
    "EVENTS",
    "EVENT_TYPES",
    "Effect",
    "EffectLedger",
    "EffectRow",
    "EffectStateError",
    "Event",
    "EventSchemaError",
    "Field",
    "JournalError",
    "JournalStore",
    "JournalTail",
    "JournalWriter",
    "Line",
    "MIGRATIONS",
    "POLL_INTERVAL_S",
    "PendingEvent",
    "PruneResult",
    "RENDERED_TYPES",
    "RUN_STATUSES",
    "Retention",
    "RunInfo",
    "RunJournal",
    "SCHEMA_VERSION",
    "SYNC_PREFIXES",
    "SYNC_TYPES",
    "SchemaTooNew",
    "SeqConflict",
    "UnknownEventType",
    "brief_args",
    "close_writer",
    "default_path",
    "get_ledger",
    "get_writer",
    "is_synchronous",
    "ledgered",
    "preview",
    "render_event",
    "retention_from_config",
    "spec_for",
    "step_id",
    "turn_ended",
    "validate_payload",
]
