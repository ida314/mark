"""Per-task measurement for the Pass 1 baseline.

Passes 8 and 9 rework the tool surface and discovery. Without numbers taken *before* that
work, a drop in completion rate afterwards is indistinguishable from noise, so this module
exists to produce those numbers and then be thrown away: Pass 2 replaces the storage with
the run journal, and this file goes with it.

Two properties it must have, in this order:

1. It never changes what the agent does. Every value here is read off state the loop was
   already keeping. Nothing feeds back into tool selection, the prompt, or the step budget.
2. It never breaks a turn. A measurement that can take the conversation down with it is
   worse than no measurement, so every failure path swallows and records rather than raises.

The unit of measurement is one `run_turn` call - one user message in, one answer out.
Sub-agent turns come through the same loop, so they are recorded too and carry `role` and
`actor`; the baseline table filters to `role == "main"`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..ids import estimate_tokens, utcnow

SCHEMA = "agentd.telemetry.turn/1"

# Write failures are counted rather than raised, and said out loud once per process. A
# silently empty telemetry file would look exactly like a session nobody ran, which is the
# one reading this pass cannot afford to get wrong.
write_failures = 0
last_error: str | None = None
_warned = False


def default_path(cfg: Config) -> Path:
    return cfg.telemetry.path or (cfg.paths.logs / "telemetry.jsonl")


@dataclass
class CallRecord:
    """One tool call, as the loop saw it resolve."""

    step: int
    name: str
    ok: bool
    denied: bool = False
    invalid_args: bool = False
    known: bool = True  # the name exists in the registry at all
    visible: bool = True  # the name was in the schema list the model was shown
    duration_ms: int = 0


@dataclass
class TurnTelemetry:
    """Accumulates one turn's measurements, then writes a single JSONL line.

    Built by `start()` and driven by the loop through the small methods below. Holding the
    partial record here rather than in loop locals is what keeps the loop edits to one line
    each, which is the only way this stays credibly read-only with respect to behavior.
    """

    request_id: str
    session_id: str
    role: str
    actor: str
    origin: str
    channel: str
    autonomy: str
    model: str
    max_steps: int
    path: Path | None
    enabled: bool = True

    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    _t0: float = field(default_factory=time.perf_counter)

    offered: list[str] = field(default_factory=list)
    visible: list[str] = field(default_factory=list)
    registry_size: int = 0
    calls: list[CallRecord] = field(default_factory=list)
    finish_reasons: list[str] = field(default_factory=list)

    context_samples: list[int] = field(default_factory=list)
    llm_ms: int = 0
    tool_ms: int = 0
    usage_reported: bool = False

    @classmethod
    def start(
        cls,
        *,
        cfg: Config,
        request_id: str,
        session_id: str,
        role: str,
        actor: str,
        origin: str,
        channel: str,
        autonomy: str,
        model: str,
    ) -> TurnTelemetry:
        return cls(
            request_id=request_id, session_id=session_id, role=role, actor=actor,
            origin=origin, channel=channel, autonomy=autonomy, model=model,
            max_steps=cfg.agent.max_steps,
            path=default_path(cfg) if cfg.telemetry.enabled else None,
            enabled=cfg.telemetry.enabled,
        )

    # --- collection ----------------------------------------------------------

    def tools_offered(self, names: list[str], *, registry_size: int) -> None:
        self.offered = sorted(names)
        self.visible = list(self.offered)
        self.registry_size = registry_size

    def tools_revealed(self, names: list[str]) -> None:
        """Tools `tool_search` pulled in mid-turn. They were not in the pre-selected set,
        which is the number Pass 9 is trying to move."""
        for name in names:
            if name not in self.visible:
                self.visible.append(name)

    def context_sample(self, messages: list[dict[str, Any]]) -> None:
        """Size of what is about to be sent to the model.

        Taken once per step, which fixes what start/peak/end mean: the first prompt of the
        turn, the largest one, and the last one. "End" is deliberately the last prompt
        rather than anything assembled after the loop - the final answer is counted in
        `usage.output_tokens` and counting it twice would make the two disagree.

        Estimated with the same `estimate_tokens` the history packer budgets against, so
        this number and the budget it is compared to are wrong in the same direction.
        """
        self.context_samples.append(messages_tokens(messages))

    def usage_seen(self, usage: dict[str, int]) -> None:
        """Whether the backend reported token counts at all.

        Not pedantry: the router in front of this model drops the `include_usage` chunk on
        streamed calls, so every streamed turn reports zero tokens. A bare 0 in the baseline
        would read as a turn that cost nothing rather than a turn nobody counted, and Pass 8
        would then be comparing a real number against a missing one.
        """
        if usage:
            self.usage_reported = True

    def llm_call(self, seconds: float, finish_reason: str | None = None) -> None:
        self.llm_ms += int(seconds * 1000)
        if finish_reason:
            self.finish_reasons.append(finish_reason)

    def tool_call(self, record: CallRecord) -> None:
        self.calls.append(record)
        self.tool_ms += record.duration_ms

    # --- derived numbers -----------------------------------------------------

    def called_names(self) -> list[str]:
        seen: list[str] = []
        for call in self.calls:
            if call.name not in seen:
                seen.append(call.name)
        return seen

    def visible_unused(self) -> list[str]:
        called = set(self.called_names())
        return [name for name in self.visible if name not in called]

    def selection_failures(self) -> dict[str, Any]:
        """Calls that went somewhere the model had to back out of.

        Four kinds, kept apart rather than summed into one number, because they have
        different fixes and Pass 8 and Pass 9 each only move some of them:

        `unknown_tool`   a name that is in no registry - invented outright.
        `not_visible`    a real tool the model reached for without being shown it. The
                         cost of the selected surface being too small.
        `invalid_args`   rejected before the handler ran, so the step bought nothing.
        `switched_after` a call failed and a later step tried a *different* tool. This is
                         the pass's "retry with a different tool", and it is a proxy: a
                         tool can fail for reasons that have nothing to do with which tool
                         was chosen. Counted separately so it can be read separately.
        """
        events: list[dict[str, Any]] = []
        for index, call in enumerate(self.calls):
            if not call.known:
                events.append({"kind": "unknown_tool", "tool": call.name, "step": call.step})
            elif not call.visible:
                events.append({"kind": "not_visible", "tool": call.name, "step": call.step})
            if call.invalid_args:
                events.append({"kind": "invalid_args", "tool": call.name, "step": call.step})
            if not call.ok and any(
                later.name != call.name and later.step > call.step
                for later in self.calls[index + 1 :]
            ):
                events.append({"kind": "switched_after", "tool": call.name, "step": call.step})
        by_kind: dict[str, int] = {}
        for event in events:
            by_kind[event["kind"]] = by_kind.get(event["kind"], 0) + 1
        return {"count": len(events), "by_kind": by_kind, "events": events}

    def record(
        self, *, status: str, steps: int, usage: dict[str, int], answer: str,
        error: str | None = None, trace_ids: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        samples = self.context_samples
        return {
            "schema": SCHEMA,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": utcnow().isoformat(),
            "role": self.role,
            "actor": self.actor,
            "origin": self.origin,
            "channel": self.channel,
            "autonomy": self.autonomy,
            "model": self.model,
            "status": status,
            "steps": steps,
            "max_steps": self.max_steps,
            "latency_ms": int((time.perf_counter() - self._t0) * 1000),
            "llm_ms": self.llm_ms,
            "tool_ms": self.tool_ms,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                # False means "not measured", not "zero". See `usage_seen`.
                "reported": self.usage_reported,
            },
            "context_tokens": {
                "start": samples[0] if samples else 0,
                "peak": max(samples) if samples else 0,
                "end": samples[-1] if samples else 0,
            },
            "tools": {
                "registry_size": self.registry_size,
                "offered": self.offered,
                "visible": self.visible,
                "called": self.called_names(),
                "visible_unused": self.visible_unused(),
                "call_count": len(self.calls),
                "calls": [vars(c) for c in self.calls],
            },
            "tool_selection_failures": self.selection_failures(),
            "finish_reasons": self.finish_reasons,
            "answer_chars": len(answer),
            "error": error,
            **(trace_ids or {}),
        }

    def finish(
        self, *, status: str, steps: int, usage: dict[str, int], answer: str,
        error: str | None = None, trace_ids: dict[str, str | None] | None = None,
    ) -> dict[str, Any] | None:
        """Build the record and append it. Returns it so tests can read it without the file."""
        if not self.enabled or self.path is None:
            return None
        try:
            row = self.record(
                status=status, steps=steps, usage=usage, answer=answer,
                error=error, trace_ids=trace_ids,
            )
            append(self.path, row)
            return row
        except Exception as exc:  # measurement must never take the turn down with it
            _note_failure(exc)
            return None


def messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimated size of a message list, counting the parts that actually cost tokens.

    Tool call arguments are included: a turn whose context is mostly the model's own
    `arguments` blobs looks small if only `content` is counted, and that is precisely the
    shape a tool-heavy turn has.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content:
            total += estimate_tokens(content)
        for call in message.get("tool_calls") or ():
            function = call.get("function") or {}
            for part in (function.get("name"), function.get("arguments")):
                if part:
                    total += estimate_tokens(str(part))
    return total


def append(path: Path, row: dict[str, Any]) -> None:
    """One record, one line, one write.

    Deliberately not a database. Pass 2 replaces this and a schema built to last would only
    have to be unbuilt. The single `write()` of a line opened O_APPEND is what keeps the CLI
    and the daemon from interleaving halves of each other's records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def read_records(path: Path) -> list[dict[str, Any]]:
    """Every complete record in the file. Used by the Pass 1c measurement run.

    A truncated final line is skipped rather than fatal: a process killed mid-write is one
    of the failure modes this pass is meant to be counting, not one it should die on.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _note_failure(exc: Exception) -> None:
    global write_failures, last_error, _warned
    write_failures += 1
    last_error = f"{type(exc).__name__}: {exc}"
    if not _warned:
        _warned = True
        print(f"(telemetry disabled for this process: {last_error})", file=sys.stderr)


__all__ = [
    "SCHEMA",
    "CallRecord",
    "TurnTelemetry",
    "append",
    "default_path",
    "messages_tokens",
    "read_records",
]
