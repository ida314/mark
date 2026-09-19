"""The controlled predicate vocabulary.

A fact's `(subject, predicate, object)` key is what makes contradiction detectable: without
it, `same_key_facts`, `conflicting_groups` and `resolve_conflicts` all fall through and the
only thing left is embedding similarity. Every fact stored before this module existed had a
null predicate, for the same reason every fact before `category` became a `Literal` came back
"other" — a defaulted free field is one the model never has to think about.

Two properties matter, and they pull in opposite directions from the `category` fix:

- **The vocabulary is closed.** `Predicate` is a flat `Literal` over every term, because that
  is what guided decoding can constrain: `complete_json` sends this schema with
  `strict: True`, and a grammar can enforce one enum but not "an enum whose members depend on
  another field". The category/family check therefore runs *after* decoding, in `coerce`.
- **An unmatched predicate becomes SQL NULL, never a bucket.** `"other"` was a legitimate
  *category* — facts genuinely can be uncategorised, and they share nothing by being so. A
  shared *predicate* is different: it is half of a grouping key, so an `other_biographical`
  bucket would make a favourite colour and a shoe size collide as the same claim about the
  same subject, and `process_candidate`'s `conflicting_key` branch would judge them as
  contradictions and supersede one with the other. NULL groups with nothing, which is the
  correct behaviour for "we do not know the relation". Inert beats wrong.
"""

from __future__ import annotations

from typing import Literal, get_args

Category = Literal[
    "biographical", "preference", "relationship", "project",
    "state", "belief", "constraint", "other",
]

#: The value the model returns when nothing fits. Stored as NULL — see the module docstring.
UNSPECIFIED = "unspecified"

Predicate = Literal[
    # biographical
    "lives_in", "born_in", "nationality", "studies_at", "works_at", "graduates_on",
    "speaks", "name_is", "timezone_is",
    # preference
    "prefers", "avoids", "communication_style_is", "tool_of_choice",
    # relationship
    "related_to", "works_with", "manages", "reports_to", "knows",
    # project
    "building", "uses_tech", "project_status_is", "deadline_is", "repo_is",
    # state
    "currently_doing", "currently_using", "located_at", "availability_is", "blocked_by",
    # belief
    "believes", "opinion_of",
    # constraint
    "must", "must_not", "budget_limit_is", "schedule_constraint", "access_limit_is",
    # none of the above
    "unspecified",
]

FAMILIES: dict[str, frozenset[str]] = {
    "biographical": frozenset({
        "lives_in", "born_in", "nationality", "studies_at", "works_at", "graduates_on",
        "speaks", "name_is", "timezone_is",
    }),
    "preference": frozenset({
        "prefers", "avoids", "communication_style_is", "tool_of_choice",
    }),
    "relationship": frozenset({
        "related_to", "works_with", "manages", "reports_to", "knows",
    }),
    "project": frozenset({
        "building", "uses_tech", "project_status_is", "deadline_is", "repo_is",
    }),
    "state": frozenset({
        "currently_doing", "currently_using", "located_at", "availability_is", "blocked_by",
    }),
    "belief": frozenset({"believes", "opinion_of"}),
    "constraint": frozenset({
        "must", "must_not", "budget_limit_is", "schedule_constraint", "access_limit_is",
    }),
    # A fact that fits no category has no relation worth keying on either.
    "other": frozenset(),
}

ALL: frozenset[str] = frozenset(get_args(Predicate)) - {UNSPECIFIED}

# Adding a predicate to the Literal without giving it a family would silently exempt it from
# every family check, so fail at import instead of at 3am in the consolidation loop.
_covered = frozenset().union(*FAMILIES.values())
if _covered != ALL:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "predicate vocabulary is inconsistent: "
        f"unfamilied={sorted(ALL - _covered)} unknown={sorted(_covered - ALL)}"
    )
if set(get_args(Category)) != set(FAMILIES):  # pragma: no cover - import-time guard
    raise RuntimeError("every category needs a family entry, even an empty one")


def family_of(predicate: str) -> str | None:
    """The category a predicate belongs to, or None if it is not in the vocabulary."""
    for category, members in FAMILIES.items():
        if predicate in members:
            return category
    return None


def coerce(predicate: str | None, category: str) -> tuple[str | None, str | None]:
    """Reconcile a decoded predicate with the category the model also chose.

    Returns `(predicate_to_store, rejected_original)`. A predicate that is unknown, is
    `unspecified`, or belongs to a different category's family is stored as None so that it
    groups with nothing; the original is returned so the mismatch stays on the record.

    Called after the model has answered, never as a pydantic validator: a `ValidationError`
    here costs a repair round-trip and then aborts the whole consolidation run.
    """
    if not predicate or predicate == UNSPECIFIED or predicate not in ALL:
        return None, (predicate if predicate and predicate != UNSPECIFIED else None)
    if predicate not in FAMILIES.get(category, frozenset()):
        return None, predicate
    return predicate, None


def vocabulary_for_prompt() -> str:
    """The vocabulary as the extraction prompt shows it, grouped by category."""
    lines = []
    for category in get_args(Category):
        members = sorted(FAMILIES[category])
        lines.append(f"  {category:13} {', '.join(members) if members else '(use unspecified)'}")
    lines.append(f"  {'any':13} {UNSPECIFIED} — when nothing above fits; a good answer")
    return "\n".join(lines)
