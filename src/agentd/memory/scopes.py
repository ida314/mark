"""The three memory buckets, as data: what each holds, where it is stored, when it ends.

Session 7b. Pass 7 asks for three logically distinct buckets - `working`, `episodic`,
`semantic` - "even if the storage backend is shared, because they have different retrieval
and update semantics". This module is that distinction, written down somewhere a test can
read it, because the alternative is three buckets that exist only in a paragraph and drift
one commit later.

    working    task-local, agent-local, short-lived, disappears with its task scope
    episodic   what happened
    semantic   what is known

## Storage: shared where it already was, and separate only where the lifecycle forces it

`episodic` and `semantic` **share the store they already shared**: the Postgres schema from
`migrations/0003_memory.sql`, `episodes` and `facts`, retrieved by one `pack()` and rendered
into one block. Nothing was migrated, nothing was split, and no column was added - the pass
file's third *Must not* is that a logical distinction is enough, and it was.

`working` is not in that store, and the reason is the lifecycle rather than a preference.
It has to be captured by a checkpoint and discarded when the run completes, and neither of
those is a thing the memory store can do: everything in Postgres here is user-scoped,
durable and append-only by trigger. So the working bucket is kept where run-scoped state
already lives - the run journal, as events, folded by `journal/working_memory.py` - which
required no migration either, in that store or in any other. Three logical buckets, two
physical stores, zero schema changes.

## Working memory does not go through retrieval, and that was decided rather than defaulted

`pack()` is the user-scoped knowledge renderer: it fuses channels with RRF, scores with
priors over `Item.kind`, and splits a token budget across four fixed sections. Putting
working memory through it would mean:

* a fifth `Item.kind` that `TYPE_PRIOR`, `CHANNEL_WEIGHTS`, `SECTION_SHARE` and `_render`'s
  section map must all learn in the same commit. There is already a `"raw"` kind in two of
  those four and in neither of the others, and an item of that kind reaching `_render`
  raises `KeyError` inside the broad `except` at `agent/loop.py:408` - which degrades to
  "(memory retrieval unavailable)" and silently costs the prompt *all* of memory. The
  existing kind proves nothing enforces that the four stay in step;
* run-scoped, worker-scoped state ranked inside a function that has no run and no worker in
  its signature. The isolation boundary would end up inside a scoring formula, where no test
  can read it and where the fallback for an unknown scope is "show it";
* scratch state competing with facts for a budget whose shares are tuned for durable
  knowledge, and inheriting `pack()`'s failure mode - the one that makes a memory failure
  invisible - for state the agent is actively using.

So working memory is read back by the agent that wrote it, through
`agent/working_memory.WorkingMemory.notes()`, and never by `pack()`. `RETRIEVAL_KINDS`
below is the set of kinds retrieval renders; `tests/test_working_memory.py` pins the working
bucket out of it, so a later pass that wants to change this decision has to change the
declaration and cannot do it by adding one dict entry.

## The episodic bucket is declared and empty

Session 7a's finding, inherited rather than fixed here: `episodes` holds **0 rows** after
111 successful consolidations, because `ExtractedEpisode` requires no fields and the writer
skips an untitled episode without counting it. That is a bug in consolidation and belongs to
whoever owns extraction. It is recorded on the bucket below as `live` so that "this bucket is
declared" and "this bucket has ever been written" stay separate claims - the distinction the
outcome record for 7a had to make in prose, because nothing in the code made it.
"""

from __future__ import annotations

from dataclasses import dataclass

WORKING = "working"
EPISODIC = "episodic"
SEMANTIC = "semantic"

# The `Item.kind` values `memory/retrieval.py` ranks and renders. The working bucket is
# deliberately absent, and its absence is tested rather than commented.
RETRIEVAL_KINDS: frozenset[str] = frozenset({"fact", "claim", "procedure", "episode"})


@dataclass(frozen=True)
class Bucket:
    """One memory bucket, described by the four things that make it distinct.

    Not an enum of names: the point of the pass is that these three differ in *semantics*,
    so the semantics are the fields. A bucket whose `store`, `scope`, `ends_with` and
    `retrieval` all matched another's would not be a third bucket.
    """

    name: str
    holds: str
    # Where the rows are. Two values today - "postgres" for the memory schema, "journal" for
    # the run journal - and shared storage is the default rather than the exception.
    store: str
    # What a row belongs to, which is what makes the bucket's lifecycle possible at all.
    scope: str
    # When the bucket's contents stop existing. "never" is the durable answer, and it is
    # spelled out rather than left as None so that no reader has to guess whether an absent
    # value means "durable" or "nobody decided".
    ends_with: str
    # Whether `memory/retrieval.pack()` ranks and renders this bucket.
    retrieval: bool
    # What writes into it today.
    writer: str
    # Whether anything has ever been written into it on the live system. See the module
    # docstring: the episodic bucket is declared, wired and empty.
    live: bool


BUCKETS: dict[str, Bucket] = {
    WORKING: Bucket(
        name=WORKING,
        holds="scratch state an agent is using to do the task in front of it",
        store="journal",
        scope="run + agent (a worker's id, or 'orchestrator')",
        ends_with="its task scope: a worker finishing, or the run completing",
        retrieval=False,
        writer="agent/working_memory.py, from the working_memory_note tool",
        live=True,
    ),
    EPISODIC: Bucket(
        name=EPISODIC,
        holds="what happened - one consolidated row per session",
        store="postgres",
        scope="user",
        ends_with="never",
        retrieval=True,
        writer="memory/consolidate.py, at post_session",
        # 0 rows after 111 consolidations. Not this session's to fix - see the docstring.
        live=False,
    ),
    SEMANTIC: Bucket(
        name=SEMANTIC,
        holds="what is known - bitemporal facts about the user",
        store="postgres",
        scope="user",
        ends_with="never",
        retrieval=True,
        writer="memory/review.py, the only writer of `facts`",
        live=True,
    ),
}

# The one bucket that is execution state rather than memory, named so that a reader does not
# have to infer it from three fields.
EXECUTION_STATE: frozenset[str] = frozenset(
    {name for name, b in BUCKETS.items() if b.ends_with != "never"}
)


def bucket(name: str) -> Bucket:
    """Look one up, loudly. An unknown bucket name is a typo, never a fourth bucket."""
    try:
        return BUCKETS[name]
    except KeyError:
        raise KeyError(
            f"unknown memory bucket {name!r}; there are exactly three: "
            f"{', '.join(sorted(BUCKETS))}"
        ) from None


__all__ = [
    "BUCKETS",
    "EPISODIC",
    "EXECUTION_STATE",
    "RETRIEVAL_KINDS",
    "SEMANTIC",
    "WORKING",
    "Bucket",
    "bucket",
]
