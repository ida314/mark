"""How much room the orchestrator has left, and when the runtime says that is not enough.

The runtime owns this. The orchestrator is never asked to monitor its own context: nothing
here reaches the model, and no message is added to a turn because of it.

**Which ceiling this is measured against, and why it is not the model window.**
`llm.max_context_tokens` is 262144 and nothing in this system assembles a prompt anywhere
near it - the structural maximum is `agent.history_tokens` (24000) plus the system block
plus `agent.max_steps` x `agent.tool_result_max_chars` of tool output, about 56k, and the
largest prompt ever observed on this machine was 6,251 estimated tokens. A threshold of
"8000 remaining" against 262144 would need a 254k prompt, which this runtime cannot build,
so it would never fire and every test of it would pass.

The ceiling that binds is `agent.history_tokens`, because it is the only budget this
runtime enforces on what carries forward: `context.history_messages` walks a session's
messages newest-first until that budget is spent and silently drops the rest. Crossing it
is the loss a handoff exists to replace with something deliberate, so the threshold sits
below it - 8000 remaining of 24000 means the runtime speaks up with a third of the window
still free, roughly 10k tokens before `history_messages` starts dropping turns.

`[handoff] ceiling_tokens` overrides that, and the model window still wins when it is the
smaller of the two, so a ceiling nobody could actually send is not one this reports against.

**Every number here is an estimate, and says so.** `ids.estimate_tokens` is len/3.2; it is
not a token count. A real count would come from the provider's `usage` chunk, and the
router in front of this model drops it - `usage_reported` has been false on every streamed
turn since Pass 1, and `actions.tokens_in` has been 0 since 2026-09-17. `ContextReading`
therefore carries `basis`, exactly as `agent_finished` carries `usage_reported`: nothing
downstream can mistake this for something that was counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config, get_config

# The journal's enum, imported rather than repeated: `agent_finished.context_basis` is
# validated against this exact tuple, and two copies of a vocabulary are two things to keep
# in step. Three values, not a bool, because "nobody counted" is a third state and folding
# it into 0 is the failure this codebase keeps having.
from ..journal.events import CONTEXT_BASES

# One estimator, one number. The journal's `context_tokens` and the telemetry record's
# `context_tokens.peak` are the same function over the same message list, so the two records
# cannot drift into disagreeing about how full a turn was.
from ..obs.telemetry import messages_tokens

BASIS_ESTIMATE, BASIS_PROVIDER, BASIS_UNMEASURED = CONTEXT_BASES

# Where the ceiling came from. Kept so a reading can say which limit it is talking about
# rather than leaving a bare number to be read as whichever one the reader had in mind.
SOURCE_HISTORY = "history_tokens"
SOURCE_CONFIGURED = "configured"
SOURCE_MODEL_WINDOW = "model_window"
CEILING_SOURCES = (SOURCE_HISTORY, SOURCE_CONFIGURED, SOURCE_MODEL_WINDOW)


@dataclass(frozen=True)
class ContextReading:
    """One look at how full the orchestrator's context is, and who said so."""

    used_tokens: int
    ceiling_tokens: int
    threshold_tokens: int
    ceiling_source: str = SOURCE_HISTORY
    basis: str = BASIS_ESTIMATE

    @property
    def remaining_tokens(self) -> int:
        """Signed, and deliberately not clamped at zero.

        A prompt larger than the ceiling is a real state - a single turn can put 30k of tool
        output in front of a 24k budget - and clamping would report it as "just full",
        which is the same shape as the plausible NULL this runtime keeps producing.
        """
        return self.ceiling_tokens - self.used_tokens

    @property
    def crossed(self) -> bool:
        return self.remaining_tokens <= self.threshold_tokens

    @property
    def measured(self) -> bool:
        return self.basis != BASIS_UNMEASURED


def ceiling(cfg: Config | None = None) -> tuple[int, str]:
    """The ceiling remaining room is measured against, and where it came from."""
    cfg = cfg or get_config()
    configured = cfg.handoff.ceiling_tokens
    if configured is None:
        value, source = cfg.agent.history_tokens, SOURCE_HISTORY
    else:
        value, source = configured, SOURCE_CONFIGURED
    window = cfg.llm.max_context_tokens
    if window < value:
        return window, SOURCE_MODEL_WINDOW
    return value, source


def threshold(cfg: Config | None = None) -> int:
    """Remaining tokens at or below which the runtime calls the context exhausted."""
    return (cfg or get_config()).handoff.threshold_tokens


def read_messages(messages: list[dict[str, Any]], *, cfg: Config | None = None) -> ContextReading:
    """Size the prompt that is about to be sent, against the ceiling that binds it.

    The whole assembled list, not the history alone: tool results and the model's own
    `arguments` blobs occupy the window while the turn runs, and a reading that ignored
    them would call a turn roomy at the moment it was least so.
    """
    cfg = cfg or get_config()
    limit, source = ceiling(cfg)
    return ContextReading(
        used_tokens=messages_tokens(messages),
        ceiling_tokens=limit,
        threshold_tokens=threshold(cfg),
        ceiling_source=source,
        basis=BASIS_ESTIMATE,
    )


def unmeasured(cfg: Config | None = None) -> ContextReading:
    """The reading for a turn that never assembled a prompt.

    Its `used_tokens` is 0 and its `basis` says why, so a fold can tell a turn that sent
    the model nothing from one nobody sized.
    """
    cfg = cfg or get_config()
    limit, source = ceiling(cfg)
    return ContextReading(
        used_tokens=0,
        ceiling_tokens=limit,
        threshold_tokens=threshold(cfg),
        ceiling_source=source,
        basis=BASIS_UNMEASURED,
    )


__all__ = [
    "BASIS_ESTIMATE",
    "BASIS_PROVIDER",
    "BASIS_UNMEASURED",
    "CEILING_SOURCES",
    "CONTEXT_BASES",
    "SOURCE_CONFIGURED",
    "SOURCE_HISTORY",
    "SOURCE_MODEL_WINDOW",
    "ContextReading",
    "ceiling",
    "read_messages",
    "threshold",
    "unmeasured",
]
