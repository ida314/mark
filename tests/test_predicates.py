"""The predicate vocabulary is half of the key that makes contradiction detectable.

The other half is the subject; see test_review_gate. What matters here is the asymmetry with
`category`: an unrecognised category is legitimately "other", but an unrecognised predicate
must become NULL rather than a shared bucket, because a predicate is a grouping key and a
bucket would make unrelated claims about the same subject look like contradictions.
"""

from __future__ import annotations

from typing import get_args

from agentd.memory import predicates as p


def test_every_predicate_belongs_to_exactly_one_category():
    seen: dict[str, str] = {}
    for category, members in p.FAMILIES.items():
        for predicate in members:
            assert predicate not in seen, f"{predicate} in both {seen.get(predicate)}/{category}"
            seen[predicate] = category
    assert set(seen) == p.ALL


def test_a_predicate_from_the_right_family_is_kept():
    assert p.coerce("lives_in", "biographical") == ("lives_in", None)


def test_a_predicate_outside_its_category_becomes_null_rather_than_a_new_key():
    stored, rejected = p.coerce("lives_in", "project")
    assert stored is None
    assert rejected == "lives_in"


def test_an_unspecified_predicate_is_stored_as_null_so_it_never_groups():
    assert p.coerce(p.UNSPECIFIED, "biographical") == (None, None)


def test_an_unknown_predicate_is_kept_on_the_record_but_not_used_as_a_key():
    stored, rejected = p.coerce("invented_by_the_model", "biographical")
    assert stored is None
    assert rejected == "invented_by_the_model"


def test_no_category_shares_an_escape_bucket_with_another():
    """The bug this guards against: an `other_biographical` key would make a favourite colour
    and a shoe size collide as one claim, and the review gate would supersede one with the
    other."""
    for category in get_args(p.Category):
        stored, _ = p.coerce(p.UNSPECIFIED, category)
        assert stored is None


def test_the_prompt_vocabulary_lists_every_predicate_the_schema_allows():
    rendered = p.vocabulary_for_prompt()
    for predicate in p.ALL:
        assert predicate in rendered
    assert p.UNSPECIFIED in rendered
