"""The delegation contract: what one agent asks another to do, in a form that does not
vary with how it was phrased.

A delegation is five things - the durable role, the task, the context the worker needs, the
constraints on it, and what it has to come back with - and `TaskSpec` is all five together.
Session 6c turns that spec into `result_key`, so the spec is a cache key before it is
anything else, and it has two opposite ways to be wrong:

* two logically identical delegations that produce different specs - the cache never hits,
  and the run pays twice for the same worker. Cheap.
* two different delegations that produce the same spec - a cache hit answers a question
  nobody asked, with a worker's result from some other piece of work. Expensive.

Normalization is therefore deliberately shallow. Whitespace, Unicode composition, ordering
and duplicates are incidental and are removed; wording, case, punctuation and numbers are
meaning and are left exactly as given. `spec_version` is part of the canonical form so that
the day these rules change, keys computed under the old ones cannot silently collide with
keys computed under the new.

The worker executor is `subagents.run_subagent`, and it takes a `TaskSpec` and nothing else.
There is no longer a way to start a worker from an ad-hoc string.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - imports for annotations only
    from uuid import UUID

    from ..config import Config
    from ..journal.writer import JournalWriter
    from ..llm.base import LLMProvider
    from ..policy.approvals import Approver
    from ..tools.registry import Registry
    from .subagents import SubagentResult, SubagentSpec

# Bumped when the normalization rules below change. It is inside the canonical form, so a
# spec normalized under one set of rules can never hash to the same key as a spec
# normalized under another.
SPEC_VERSION = 1

FIELDS: tuple[str, ...] = (
    "durable_role", "task", "relevant_context", "constraints", "expected_output",
)


class DelegationError(RuntimeError):
    """A delegation that cannot be expressed as a spec. Raised, never approximated."""


class UnknownRole(DelegationError):
    """A durable role nobody registered.

    Refused rather than routed to a default worker: a spec naming a role that does not exist
    would still hash, and 6c would then cache a result under a key that claims a role the
    run never had.
    """


class EmptyTask(DelegationError):
    """A delegation with nothing in it. A worker cannot be given "" and report honestly."""


_SPACES = re.compile(r"[ \t   ]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


def normalize_text(value: str) -> str:
    """The one normalization every field goes through.

    NFC first, because two strings that render identically but compose differently are the
    same delegation to the person who typed them and different bytes to sha256. Then line
    endings, then runs of horizontal whitespace, then trailing whitespace per line, then
    runs of blank lines. Nothing here touches a word, a letter case, a digit or a mark of
    punctuation.
    """
    if not isinstance(value, str):
        raise DelegationError(f"a delegation field must be a string, got {type(value).__name__}")
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n", text).strip()


def _items(value: Sequence[str] | str | None) -> tuple[str, ...]:
    """A set-valued field: deduplicated and sorted, so the order it was written in is not
    part of the key.

    The cost, stated where it is paid: a caller who means "first do A, then B" must say so
    in `task`. `relevant_context` and `constraints` are things that are true of the
    delegation, not steps in it.

    A bare string is one item and not a sequence of characters - the classic iteration bug,
    and the shape the `delegate` tool passes, because its `context` argument is prose.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    items: set[str] = set()
    for entry in value:
        text = normalize_text(entry)
        if text:
            items.add(text)
    return tuple(sorted(items))


@dataclass(frozen=True)
class TaskSpec:
    """One delegation, normalized at construction.

    Normalization is in `__post_init__` rather than in a builder function so that there is no
    way to hold an un-normalized spec: a caller who constructs one directly gets the same
    object `delegate()` would have built from the same words.
    """

    durable_role: str
    task: str
    relevant_context: tuple[str, ...] | Sequence[str] | str = ()
    constraints: tuple[str, ...] | Sequence[str] | str = ()
    expected_output: str = ""

    def __post_init__(self) -> None:
        role = normalize_text(self.durable_role)
        spec = role_spec(role)
        task = normalize_text(self.task)
        if not task:
            raise EmptyTask(
                f"delegation to {role!r} has no task: a worker cannot see the conversation "
                "this came from, so an empty brief is nothing it can act on"
            )
        # An omitted `expected_output` falls back to the role's declared reporting contract,
        # and the fallback is written *into the spec* - so it is in the canonical form, in
        # the digest and in the journal, rather than being applied later by whoever renders
        # the brief. A default that is invisible in the record is the one that goes wrong.
        expected = normalize_text(self.expected_output) or normalize_text(spec.expected_output)
        object.__setattr__(self, "durable_role", role)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "relevant_context", _items(self.relevant_context))
        object.__setattr__(self, "constraints", _items(self.constraints))
        object.__setattr__(self, "expected_output", expected)

    # --- identity ------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_version": SPEC_VERSION,
            "durable_role": self.durable_role,
            "task": self.task,
            "relevant_context": list(self.relevant_context),
            "constraints": list(self.constraints),
            "expected_output": self.expected_output,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        version = data.get("spec_version")
        if version != SPEC_VERSION:
            raise DelegationError(
                f"task spec version {version!r} was written by other normalization rules; "
                f"this build reads version {SPEC_VERSION}"
            )
        return cls(
            durable_role=data["durable_role"],
            task=data["task"],
            relevant_context=tuple(data.get("relevant_context") or ()),
            constraints=tuple(data.get("constraints") or ()),
            expected_output=data.get("expected_output") or "",
        )

    def canonical(self) -> str:
        """The bytes a key is taken over. Sorted keys, no incidental spacing, UTF-8 as-is."""
        return json.dumps(
            self.as_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )

    @property
    def digest(self) -> str:
        """sha256 of the canonical form.

        6c's `result_key = hash(durable_role, task_spec, relevant_context_refs)` is a hash of
        three things that are all inside this one, so it can be this digest or a hash of it
        with the run id; what it must not be is a second, independent derivation.
        """
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    # --- what the worker is handed -------------------------------------------

    @property
    def brief(self) -> str:
        """The spec as the text the worker receives.

        Derived from the spec and only from the spec, so two identical specs hand identical
        text to the worker. A worker cannot see the conversation the delegation came from,
        which is why the context and the constraints are in the message rather than assumed.
        """
        blocks = [self.task]
        if self.relevant_context:
            blocks.append(
                "Context you have been given (you cannot see the conversation this came "
                "from):\n" + "\n".join(f"- {item}" for item in self.relevant_context)
            )
        if self.constraints:
            blocks.append("Constraints:\n" + "\n".join(f"- {item}" for item in self.constraints))
        if self.expected_output:
            blocks.append(f"Report back:\n{self.expected_output}")
        return "\n\n".join(blocks)


def role_names() -> tuple[str, ...]:
    """The durable roles a delegation may name, from the one registry that defines them."""
    from .subagents import SPECS

    return tuple(sorted(SPECS))


def role_spec(name: str) -> SubagentSpec:
    """The role's configuration, or `UnknownRole`. Never a fallback role."""
    from .subagents import SPECS

    spec = SPECS.get(name)
    if spec is None:
        raise UnknownRole(
            f"no durable role named {name!r}; this runtime has {', '.join(role_names())}"
        )
    return spec


async def delegate(
    durable_role: str,
    task: str,
    *,
    relevant_context: Sequence[str] | str = (),
    constraints: Sequence[str] | str = (),
    expected_output: str = "",
    parent_session_id: UUID,
    parent_turn_id: UUID,
    parent_autonomy: str,
    approver: Approver,
    registry: Registry | None = None,
    cfg: Config | None = None,
    parent_action_id: UUID | None = None,
    parent_run_id: str | None = None,
    parent_step_id: str | None = None,
    journal: JournalWriter | None = None,
    provider: LLMProvider | None = None,
) -> SubagentResult:
    """Start one durable-role worker on one task specification, and wait for its report.

    The five leading parameters are the delegation. Everything after them is the plumbing of
    the run the delegation is made inside, and none of it is part of the spec or of the key:
    a delegation is the same delegation whichever step of whichever turn made it.

    The worker runs its whole workflow before this returns. There is no mechanism for it to
    hand control back after a tool call, which is the architecture's "subagents complete an
    entire coherent workflow" as a property of the executor rather than as an instruction in
    a prompt.
    """
    spec = TaskSpec(
        durable_role=durable_role,
        task=task,
        relevant_context=relevant_context,
        constraints=constraints,
        expected_output=expected_output,
    )
    from .subagents import run_subagent

    return await run_subagent(
        role_spec(spec.durable_role),
        spec,
        parent_session_id=parent_session_id,
        parent_turn_id=parent_turn_id,
        parent_autonomy=parent_autonomy,
        approver=approver,
        registry=registry,
        cfg=cfg,
        parent_action_id=parent_action_id,
        parent_run_id=parent_run_id,
        parent_step_id=parent_step_id,
        journal=journal,
        provider=provider,
    )
