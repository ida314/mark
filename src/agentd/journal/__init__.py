"""The run journal: one append-only event log per run, and the only event path there is.

`state = fold(reduce, journal, initial)`. Session 2a built the store and the writer; 2b added
the event vocabulary (`events.py`) and the per-run handle the runtime writes through
(`runtime.RunJournal`); 2c added the subscription (`feed.py`), the shared rendering
(`render.py`), and deleted the in-process event bus that used to carry the same facts to the
CLI and to Telegram.

Session 4a added `checkpoints.py`: a mechanical snapshot of where a run had got to, written
at five boundaries when `[checkpoints] enabled` is on. Session 4b added `resume.py`, which
is the fold: it rebuilds a run's message list from the journal, reconciles the effects a
crash left open by effect class, and announces itself with `run_resumed`. It reads a
checkpoint when there is one and does not need one, so a run recorded before the flag was
ever switched on resumes the same way.

`resume.plan()` and `resume.resume()` are deliberately *not* re-exported here: binding a
function called `resume` on this package would shadow the module of the same name, and
`from agentd.journal import resume` would then hand back a function to anyone who wanted the
module. Import them from `agentd.journal.resume` directly.
"""

from .checkpoints import (
    TRIGGERS,
    Checkpoint,
    Checkpointer,
    CheckpointError,
    EffectsCursor,
    MessagesRef,
    MidWorkerCheckpoint,
    NoOrchestrator,
    WorkerRef,
    checkpoint_at,
    get_checkpointer,
)
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
from .resume import (
    COMPLETE,
    DISPOSITIONS,
    INTERRUPTED,
    NO_ORCHESTRATOR,
    RETRY,
    UNCERTAIN,
    NoSuchRun,
    Orphan,
    Reconciliation,
    Rehydration,
    Resumed,
    ResumeError,
    ResumePlan,
    UncertainGroup,
)
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
    "COMPLETE",
    "Checkpoint",
    "CheckpointError",
    "Checkpointer",
    "DISPOSITIONS",
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
    "EffectsCursor",
    "Event",
    "EventSchemaError",
    "Field",
    "INTERRUPTED",
    "JournalError",
    "JournalStore",
    "JournalTail",
    "JournalWriter",
    "Line",
    "MIGRATIONS",
    "MessagesRef",
    "MidWorkerCheckpoint",
    "NO_ORCHESTRATOR",
    "NoOrchestrator",
    "NoSuchRun",
    "Orphan",
    "POLL_INTERVAL_S",
    "PendingEvent",
    "PruneResult",
    "RENDERED_TYPES",
    "RETRY",
    "RUN_STATUSES",
    "Reconciliation",
    "Rehydration",
    "ResumeError",
    "ResumePlan",
    "Resumed",
    "Retention",
    "RunInfo",
    "RunJournal",
    "SCHEMA_VERSION",
    "SYNC_PREFIXES",
    "SYNC_TYPES",
    "SchemaTooNew",
    "SeqConflict",
    "TRIGGERS",
    "UNCERTAIN",
    "UncertainGroup",
    "UnknownEventType",
    "WorkerRef",
    "brief_args",
    "checkpoint_at",
    "close_writer",
    "default_path",
    "get_checkpointer",
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
