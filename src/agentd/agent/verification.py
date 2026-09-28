"""What a worker actually did, and whether its report can be squared with it.

`results.py` decides whether a worker returned *a result*. This module decides whether that
result is *true* - as far as the run journal can say - and the difference is the pass.
Between 8a and 8d a worker's report was measured wrong four times in two days, in both
directions, and every one of those reports satisfied the schema:

    B10   cited three file paths        eleven shell_exec calls had found the real ones;
                                        the three cited files do not exist
    B11   "No work was performed"       fifteen tool calls read the repo; report_valid=True
    B12   "all 19 tests pass"           2 of 19 failed, re-run on the same commit
    B16   "no web access from that      zero tool calls in that run; the same role made ten
           context"                     successful web calls minutes earlier

plus a fifth shape 8d found without naming it: four rows emitted tool-call markup as prose,
and the runtime recorded `status=completed, steps=1` without noticing that nothing ran.

The report is a second inference over `text[:20000]` made by a model that no longer has the
tool results in front of it. The runtime is not in that position: the worker's own journal
records every call it requested, with the arguments, and how each one ended. So the check
costs nothing but arithmetic, and the pass file makes that binding - **no model call in this
path**. A verifier that costs an inference is a second thing that can be wrong about the
same transcript, and it would be wrong in the same way, because it would be the same model
reading the same truncation.

Two rules govern what may be flagged:

1. **Every check names the journal event it read.** A check over the answer text alone is a
   grader, not a verifier; it would fire on style and be switched off within a week.
2. **A flag is runtime-written, in full.** Details quote the journal - a tool name, an exit
   code, a path - and never the model's prose. That is what makes a flag safe to put in
   front of the orchestrator, and it is the same rule `results.py` keeps for `notes`.

Hard flags mean `invalidated`: a contradiction, not a doubt. Soft flags mean `uncertain`.
The three-way reading is deliberately not a number - a score would be tuned until it stopped
firing, which is how this kind of check dies.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ..journal.store import Event
from .results import Flag, WorkerResult

# The shape of what `worker_verified` carries. A later session that changes which checks run
# or what a flag means bumps this, so a fold can skip an entry written under other rules
# rather than read it as if the rules had not moved - the same gate `worker_result_cached`
# has had since 6c.
ENTRY_VERSION = 1

SHELL = "shell_exec"
WRITE_TOOLS = frozenset({"fs_write"})

# What a test run looks like on the command line. Deliberately a list of runners rather than
# the word "test": `shell_exec "cat tests/test_loop.py"` is not a test run, and a worker that
# claims a green suite after reading the test file is exactly the B12 shape.
TEST_RUNNERS = re.compile(
    r"\b(pytest|unittest|nose2|tox|jest|vitest|rspec|phpunit|ctest"
    r"|go\s+test|cargo\s+test|mix\s+test|(npm|pnpm|yarn|bun)\s+(run\s+)?test"
    r"|make\s+(test|check))\b"
)
# A claim that a test run passed. Both orders, because a model writes either.
CLAIMS_TESTS_PASSED = re.compile(
    r"\b("
    r"(all\s+)?(\d+\s+)?(tests?|specs?|suite|checks?)\b[^.\n]{0,40}\b"
    r"(pass(es|ed|ing)?|green|succeed(ed|s)?|ok)\b"
    r"|(pass(es|ed|ing)?|green)\b[^.\n]{0,20}\b(tests?|specs?|suite)\b"
    r"|\b\d+\s+passed\b"
    r")",
    re.IGNORECASE,
)
# A claim that the worker could not get at something. Matched against the model's own answer
# and followups only - never against a runtime note, which is why `verify` refuses to run at
# all on a result whose report was unreadable.
CLAIMS_NO_ACCESS = re.compile(
    r"(could\s?n[o']t|cannot|can\s?not|can't|unable\s+to|no\s+way\s+to|do(es)?\s+not\s+have)"
    r"[^.\n]{0,40}\b(access|reach|fetch|open|read|browse|connect|retriev)",
    re.IGNORECASE,
)
# A claim that something outside the worker changed.
CLAIMS_WROTE = re.compile(
    r"\b(edit(ed|s)?|wrote|writ(e|ten)|creat(e|ed)|add(ed)?|modif(y|ied)|patch(ed)?"
    r"|delet(e|ed)|renam(e|ed)|updat(e|ed)|append(ed)?|fix(ed)?)\b",
    re.IGNORECASE,
)

# A path-shaped token: something with a separator in it, or a bare filename with a suffix
# this repo's work actually produces. Both halves are needed - "src/agentd/loop.py" has a
# separator, "pyproject.toml" does not - and a token with neither is prose.
PATH_LIKE = re.compile(
    r"(?<![\w/])(?:~?/|\.{1,2}/)?[\w.-]+(?:/[\w.-]+)+"
    r"|(?<![\w/.])[\w-]+\.(?:py|md|toml|yaml|yml|json|ts|tsx|js|jsx|sql|sh|txt|cfg|ini|lock|rs|go)\b"
)
URL_LIKE = re.compile(r"https?://[^\s)\]>,]+")
EXIT_LINE = re.compile(r"^exit=(-?\d+)")

# The codes, in one place, because they are written into the journal and read back by
# `builtin_delegate` and by 9d's measurement.
TESTS_NOT_RUN = "tests_not_run"
FILE_NOT_READ = "file_not_read"
STATUS_CONFLICT = "status_conflict"
NO_ATTEMPT = "no_attempt"
COMPLETED_WITHOUT_TOOLS = "completed_without_tools"
WRITE_NOT_PERFORMED = "write_not_performed"

HARD_FLAGS: frozenset[str] = frozenset({TESTS_NOT_RUN, FILE_NOT_READ, STATUS_CONFLICT})
FLAG_CODES: tuple[str, ...] = (
    TESTS_NOT_RUN,
    FILE_NOT_READ,
    STATUS_CONFLICT,
    NO_ATTEMPT,
    COMPLETED_WITHOUT_TOOLS,
    WRITE_NOT_PERFORMED,
)


@dataclass(frozen=True)
class ToolCall:
    """One call, joined from the two or three events the journal wrote for it.

    `ok` is `None` rather than `False` for a call that was requested and never terminated -
    the process died, or the turn was cancelled mid-call. It is not a failure and it is
    emphatically not a success, and a tri-state here is what keeps a crashed call from
    counting as evidence that the work was done.
    """

    name: str
    args: dict = field(default_factory=dict)
    ok: bool | None = None
    error: str = ""
    exit_code: int | None = None
    # The approval this call was parked under, from `tool_failed.queued_id`; "" when it was
    # not queued. A queued call is a failed one with a person's decision pending on it.
    queued_id: str = ""

    @property
    def command(self) -> str:
        return str(self.args.get("command", "")) if self.name == SHELL else ""


@dataclass(frozen=True)
class WorkerLedger:
    """What the journal says one worker did.

    Everything here was read off `tool_requested` / `tool_finished` / `tool_failed` /
    `agent_finished` for a single `worker_id`. Nothing is inferred from the worker's prose,
    which is the whole point: this is the side of the comparison the model did not write.
    """

    calls: tuple[ToolCall, ...] = ()
    turn_status: str = ""
    steps: int = 0
    files_touched: frozenset[str] = frozenset()
    urls_touched: frozenset[str] = frozenset()

    @property
    def failures(self) -> int:
        return sum(1 for c in self.calls if c.ok is False)

    @property
    def denials(self) -> int:
        return sum(1 for c in self.calls if c.ok is False and c.args.get("__denied__"))

    @property
    def queued_approvals(self) -> tuple[str, ...]:
        """Approval ids this worker's calls are waiting under, in call order, once each."""
        seen: list[str] = []
        for call in self.calls:
            if call.queued_id and call.queued_id not in seen:
                seen.append(call.queued_id)
        return tuple(seen)

    @property
    def shell_runs(self) -> tuple[ToolCall, ...]:
        return tuple(c for c in self.calls if c.name == SHELL)

    @property
    def last_shell_exit(self) -> int | None:
        for call in reversed(self.shell_runs):
            if call.exit_code is not None:
                return call.exit_code
        return None

    def succeeded(self, *names: str) -> tuple[ToolCall, ...]:
        wanted = frozenset(names)
        return tuple(c for c in self.calls if c.ok and c.name in wanted)


def _exit_code(text: str) -> int | None:
    """The `exit=N` prefix `shell_exec` writes, or None.

    A timed-out command has no exit code at all (`builtin_shell.py` kills the container and
    returns a sentence), so this is genuinely absent rather than zero - and reading an absent
    exit code as 0 would turn a killed test run into a passing one.
    """
    match = EXIT_LINE.match(text.strip())
    return int(match.group(1)) if match else None


def _path_args(registry_paths: Mapping[str, Sequence[str]] | None) -> Mapping[str, Sequence[str]]:
    """Which argument of which tool names a path.

    Taken from `Tool.path_args`, which the policy engine's root check already reads, so this
    module does not keep a second list of argument names that can drift from the first.
    """
    if registry_paths is not None:
        return registry_paths
    from ..tools.registry import get_registry

    return {name: tool.path_args for name, tool in get_registry().tools.items() if tool.path_args}


def ledger_from_events(
    events: Iterable[Event],
    *,
    worker_id: str | None = None,
    path_args: Mapping[str, Sequence[str]] | None = None,
) -> WorkerLedger:
    """Fold a worker's journal events into what it did.

    Pure, and takes events rather than a store, so the same function serves a live worker
    (`subagents.py` already drains a worker-scoped tail), a replay, and a test that writes
    five events by hand. `worker_id` narrows a whole run's events to one worker; events that
    are already narrowed pass through unchanged.
    """
    paths = _path_args(path_args)
    pending: dict[str, ToolCall] = {}
    order: list[str] = []
    calls: dict[str, ToolCall] = {}
    turn_status = ""
    steps = 0
    files: set[str] = set()
    urls: set[str] = set()

    for event in events:
        payload = event.payload
        if worker_id is not None and payload.get("worker_id") != worker_id:
            continue
        if event.type == "tool_requested":
            name = payload["name"]
            args = dict(payload.get("args") or {})
            call = ToolCall(name=name, args=args)
            pending[payload["call_id"]] = call
            order.append(payload["call_id"])
            calls[payload["call_id"]] = call
            for key in paths.get(name, ()):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    files.add(value.strip())
            for value in args.values():
                if isinstance(value, str):
                    urls.update(URL_LIKE.findall(value))
        elif event.type == "tool_finished":
            call = pending.pop(payload["call_id"], None)
            if call is None:
                continue
            summary = str(payload.get("summary") or "")
            calls[payload["call_id"]] = ToolCall(
                name=call.name,
                args=call.args,
                ok=True,
                exit_code=_exit_code(summary) if call.name == SHELL else None,
            )
        elif event.type == "tool_failed":
            call = pending.pop(payload["call_id"], None)
            if call is None:
                continue
            error = str(payload.get("error") or "")
            args = dict(call.args)
            if payload.get("denied"):
                # Carried on the call rather than in a second list: a denial is a failed call
                # with a flag on it in the journal too (`tool_failed.denied`), and keeping the
                # two shapes the same means one fold, not two.
                args["__denied__"] = True
            calls[payload["call_id"]] = ToolCall(
                name=call.name,
                args=args,
                ok=False,
                error=error,
                exit_code=_exit_code(error) if call.name == SHELL else None,
                queued_id=str(payload.get("queued_id") or ""),
            )
        elif event.type == "agent_finished":
            turn_status = payload.get("status", "")
            steps = int(payload.get("steps", 0))

    return WorkerLedger(
        calls=tuple(calls[cid] for cid in order),
        turn_status=turn_status,
        steps=steps,
        files_touched=frozenset(files),
        urls_touched=frozenset(urls),
    )


def _grounded(token: str, ledger: WorkerLedger, brief: str) -> bool:
    """Did this worker come anywhere near this path?

    Suffix matching in both directions, because a worker reads `/home/dylan/Projects/agent/
    src/agentd/agent/loop.py` and cites `src/agentd/agent/loop.py`, and either string may be
    the longer one. A path named in the brief is grounded by definition - the caller told the
    worker about it, so citing it back is not a fabrication even if the worker never opened
    it, and flagging that would fire on every delegation that names a file.
    """
    needle = token.strip().rstrip(":,;.").lstrip("./")
    if not needle:
        return True
    if needle in brief:
        return True
    for touched in ledger.files_touched:
        clean = touched.rstrip("/")
        if clean.endswith(needle) or needle.endswith(clean.lstrip("./")):
            return True
    return any(needle in call.command for call in ledger.shell_runs)


def verify(result: WorkerResult, ledger: WorkerLedger, *, brief: str = "") -> tuple[Flag, ...]:
    """Check one report against one ledger. No model call, no network, no I/O.

    Runs only on a report the runtime could read. When `report_valid` is False there is no
    model claim to check - the answer is a sentence `results.py` wrote - and running the text
    checks against the runtime's own wording is how a verifier ends up flagging itself.
    """
    if not result.report_valid:
        return ()

    flags: list[Flag] = []
    claimed = " ".join((result.answer, *result.evidence, *result.actions_taken))
    completed = result.status == "completed"

    # B12. A green test run is the single most consequential thing a coder can claim, and it
    # is the one claim the journal can always check: a test ran in the sandbox or it did not.
    if completed and CLAIMS_TESTS_PASSED.search(claimed):
        green = [c for c in ledger.shell_runs if c.ok and TEST_RUNNERS.search(c.command)]
        if not green:
            attempted = [c for c in ledger.shell_runs if TEST_RUNNERS.search(c.command)]
            if attempted:
                detail = (
                    f"a passing test run is claimed, but the {len(attempted)} shell_exec "
                    f"call(s) that ran a test command all failed; the last exited "
                    f"{ledger.last_shell_exit}"
                )
            else:
                detail = (
                    "a passing test run is claimed and no shell_exec call ran a test command "
                    f"({len(ledger.shell_runs)} shell_exec call(s) in this worker)"
                )
            flags.append(Flag(code=TESTS_NOT_RUN, detail=detail, hard=True))

    # B10. Evidence is the part of the schema that is supposed to be checkable, and a citation
    # naming a file the worker never touched is the one fabrication that costs a reader most:
    # it looks exactly like the real thing.
    if completed and result.evidence:
        ungrounded: list[str] = []
        for line in result.evidence:
            for token in PATH_LIKE.findall(URL_LIKE.sub(" ", line)):
                if not _grounded(token, ledger, brief) and token not in ungrounded:
                    ungrounded.append(token)
        if ungrounded:
            named = ", ".join(ungrounded[:3])
            more = f" (and {len(ungrounded) - 3} more)" if len(ungrounded) > 3 else ""
            flags.append(
                Flag(
                    code=FILE_NOT_READ,
                    detail=(
                        f"cited as evidence but never opened by this worker and not named in "
                        f"the brief: {named}{more}"
                    ),
                    hard=True,
                )
            )

    # B11. The worker's own turn ended badly and its report says otherwise. `abandoned` is not
    # here: `results.py` already forces that to `uncertain` through `budget_exhausted`, and
    # flagging it a second time would double-count the same fact.
    if completed and ledger.turn_status in ("failed", "cancelled"):
        flags.append(
            Flag(
                code=STATUS_CONFLICT,
                detail=(
                    f"the report says completed; this worker's own turn ended "
                    f"'{ledger.turn_status}' after {ledger.steps} step(s)"
                ),
                hard=True,
            )
        )

    # B16. "I could not reach it" from a worker that never tried. Keyed on zero calls rather
    # than on zero calls of some family, because zero is the only count that needs no judgement
    # about which tool would have been the right one.
    said_blocked = CLAIMS_NO_ACCESS.search(" ".join((result.answer, *result.followups)))
    if said_blocked and not ledger.calls:
        flags.append(
            Flag(
                code=NO_ATTEMPT,
                detail=(
                    "the report says something could not be accessed; this worker made no "
                    "tool calls at all"
                ),
                hard=False,
            )
        )
    elif completed and not ledger.calls:
        # 8d's four rows: tool-call markup emitted as prose, `status=completed`, `steps=1`,
        # and nothing in the runtime noticed. Soft rather than hard because a brief that is
        # answerable from its own text is a legitimate, if rare, completion with no calls.
        flags.append(
            Flag(
                code=COMPLETED_WITHOUT_TOOLS,
                detail=(
                    f"completed after {ledger.steps} step(s) with no tool calls; nothing in "
                    "the journal shows work being done"
                ),
                hard=False,
            )
        )

    # Symmetric with the test check, on the other side of the same role: a change claimed with
    # nothing in the journal that could have made one.
    if completed and result.actions_taken and CLAIMS_WROTE.search(" ".join(result.actions_taken)):
        wrote = ledger.succeeded(*WRITE_TOOLS)
        ran = [c for c in ledger.shell_runs if c.ok]
        if not wrote and not ran:
            flags.append(
                Flag(
                    code=WRITE_NOT_PERFORMED,
                    detail=(
                        "actions_taken claims a change; this worker made no successful "
                        "fs_write or shell_exec call"
                    ),
                    hard=False,
                )
            )

    return tuple(flags)


def validation_of(flags: Sequence[Flag]) -> str:
    """The three-way reading. `invalidated` is a contradiction; `uncertain` is a doubt."""
    if any(f.hard for f in flags):
        return "invalidated"
    return "uncertain" if flags else "valid"
