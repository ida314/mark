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

**Two readings, two questions (session 5b).** `read_messages` sizes the whole assembled
prompt: tool results and the model's own `arguments` blobs are in the window while a turn
runs, so it is the honest answer to "is this prompt near the ceiling". `carried` sizes only
what survives into the next turn. They differ by exactly the in-turn material, which is
bounded by `max_steps` x `tool_result_max_chars` and is gone by the next prompt - so a turn
can cross on the first reading with a two-message conversation behind it. The threshold
crossing that marks the `handoff` checkpoint is the first reading; the one that decides a
handoff is generated is the second.

**Every number here is an estimate, and says so.** `ids.estimate_tokens` is len/3.2; it is
not a token count. A real count would come from the provider's `usage` chunk, and the
router in front of this model drops it - `usage_reported` has been false on every streamed
turn since Pass 1, and `actions.tokens_in` has been 0 since 2026-09-17. `ContextReading`
therefore carries `basis`, exactly as `agent_finished` carries `usage_reported`: nothing
downstream can mistake this for something that was counted.
"""

from __future__ import annotations

from collections.abc import Sequence
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


# The roles whose text carries into the next turn. `context.history_messages` rebuilds a
# conversation from the archive's `user_message`, `assistant_message` and `tool_result`
# rows: since 2026-09-28 a turn's tool calls are replayed (clipped) before its answer, so a
# `tool` message and an assistant message that holds only `tool_calls` both carry. A system
# block is rebuilt from scratch every turn and the runtime's own mid-turn notes are never
# archived at all, so those still do not. What is listed here is exactly what
# `agent.history_tokens` is spent on, and a reading over it is the only one comparable to
# that ceiling.
CARRIED_ROLES = ("user", "assistant", "tool")


def carried_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset of a message list that will still exist on the next turn, at the size it
    will exist at: a tool result is replayed clipped to `HISTORY_TOOL_RESULT_CHARS`, so a
    64KB listing in this turn's prompt carries as a few hundred characters, not as 64KB."""
    from .context import HISTORY_TOOL_RESULT_CHARS

    kept: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") not in CARRIED_ROLES:
            continue
        content = m.get("content") or ""
        if not (content.strip() or m.get("tool_calls")):
            continue
        if m.get("role") == "tool" and len(content) > HISTORY_TOOL_RESULT_CHARS:
            m = {**m, "content": content[:HISTORY_TOOL_RESULT_CHARS]}
        kept.append(m)
    return kept


def carried(messages: Sequence[dict[str, Any]], *, cfg: Config | None = None) -> ContextReading:
    """Size what carries forward, against the ceiling that governs carrying forward.

    Session 5a measured the whole assembled prompt and handed the consequence to 5b: a turn
    with six full-size tool results crosses on prompt size while its *conversation* is two
    messages long. Both readings are true and they answer different questions.
    `read_messages` answers "is this prompt near the ceiling"; this answers "is this
    conversation too long to carry", which is the question a handoff exists for - in-turn
    tool output is bounded by `max_steps` and is gone by the next turn, so compressing a
    conversation because of it would be the lossy path taken where the lossless one fits.
    """
    cfg = cfg or get_config()
    limit, source = ceiling(cfg)
    return ContextReading(
        used_tokens=messages_tokens(carried_messages(messages)),
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
    "CARRIED_ROLES",
    "CEILING_SOURCES",
    "CONTEXT_BASES",
    "SOURCE_CONFIGURED",
    "SOURCE_HISTORY",
    "SOURCE_MODEL_WINDOW",
    "ContextReading",
    "carried",
    "carried_messages",
    "ceiling",
    "read_messages",
    "threshold",
    "unmeasured",
]
