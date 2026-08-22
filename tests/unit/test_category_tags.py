"""Category vocabulary and the fail-loud behaviour on an unknown one.

Regression tests for a silent failure found by running the pipeline for
real: the map knew four categories, none of them the ordinary word
"dentist", and an unmapped category contributed zero clauses to the
Overpass query. The query stayed syntactically valid with an empty body,
OSM returned nothing, and the run reported success with 0 results —
indistinguishable from "there are no dentists in Austin".
"""

from __future__ import annotations

import pytest

from app.resolvers.discovery.category_tags import (
    UnknownCategoryError,
    load_category_tags,
    normalise,
    tags_for,
)


def test_ordinary_trade_words_are_mapped() -> None:
    """A person types the word they use, not OSM's word for it."""
    tags = load_category_tags()
    for category in ("dentist", "plumber", "electrician", "hair salon", "restaurant"):
        assert tags_for(category, tags), f"{category!r} must map to at least one tag set"


def test_synonyms_resolve_to_the_same_tags() -> None:
    tags = load_category_tags()
    assert tags_for("dentist", tags) == tags_for("dental clinic", tags)
    assert tags_for("vet", tags) == tags_for("veterinary", tags)


def test_lookup_ignores_case_and_extra_whitespace() -> None:
    """The category arrives from a text box a human typed into."""
    tags = load_category_tags()
    expected = tags_for("dental clinic", tags)

    assert tags_for("Dental Clinic", tags) == expected
    assert tags_for("  DENTAL   clinic  ", tags) == expected


def test_unknown_category_raises_rather_than_returning_nothing() -> None:
    """The bug: an unmapped category used to be skipped, producing an
    empty query and a successful-looking run with zero results.
    """
    tags = load_category_tags()

    with pytest.raises(UnknownCategoryError) as caught:
        tags_for("artisanal yak grooming", tags)

    message = str(caught.value)
    assert "artisanal yak grooming" in message
    assert "return nothing" in message, "must say why the search would be empty"


def test_unknown_category_suggests_close_matches() -> None:
    """A typo should be recoverable without reading the source."""
    tags = load_category_tags()

    with pytest.raises(UnknownCategoryError) as caught:
        tags_for("dentst", tags)

    assert "dentist" in caught.value.suggestions
    assert "Did you mean" in str(caught.value)


def test_metadata_keys_are_not_categories() -> None:
    """The data file documents itself with a _comment key, which must not
    become a searchable category.
    """
    tags = load_category_tags()
    assert not [key for key in tags if key.startswith("_")]


def test_every_mapped_category_has_usable_tags() -> None:
    """A category present but mapped to an empty list would reintroduce
    the original bug for that one word.
    """
    tags = load_category_tags()
    assert len(tags) > 50, "the vocabulary should cover ordinary local businesses"

    for category, tag_sets in tags.items():
        assert tag_sets, f"{category!r} maps to no tags"
        for tag_set in tag_sets:
            assert tag_set, f"{category!r} has an empty tag set"
            for key, value in tag_set.items():
                assert key, f"{category!r} has a tag with a blank key"
                assert value, f"{category!r} has tag {key!r} with a blank value"


def test_normalise_is_idempotent() -> None:
    assert normalise(normalise("  Dental   Clinic ")) == "dental clinic"
