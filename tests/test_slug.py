import re

import pytest

from imp.engine.slug import (
    MAX_SLUG_LEN,
    InvalidSlug,
    dated_slug,
    slugify,
    split_date_prefix,
)


def test_basic() -> None:
    assert slugify("Add OAuth support") == "add-oauth-support"


def test_long_title_is_capped_at_word_boundary() -> None:
    # A long title must not produce an over-length git branch ref.
    title = "Edit src/imp/agents/tools/bash.py and add a sentinel print " \
            "as the first statement inside the bash function do not refactor"
    s = slugify(title)
    assert len(s) <= MAX_SLUG_LEN
    assert s and not s.endswith("-") and not s.startswith("-")
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", s)
    # The capped slug is a clean prefix of the uncapped derivation (cut at a
    # word boundary, so it ends on a whole token).
    uncapped = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")
    assert uncapped.startswith(s)
    assert uncapped[len(s)] == "-"  # we cut exactly at a boundary


def test_single_oversized_token_hard_truncates() -> None:
    s = slugify("x" * 200)
    assert len(s) == MAX_SLUG_LEN
    assert s == "x" * MAX_SLUG_LEN


def test_short_titles_unaffected_by_cap() -> None:
    assert slugify("brave silver otter") == "brave-silver-otter"


def test_collapses_and_trims() -> None:
    assert slugify("  Foo / Bar!! ") == "foo-bar"
    assert slugify("a---b") == "a-b"


def test_three_word_phrase() -> None:
    assert slugify("brave silver otter") == "brave-silver-otter"


def test_empty_after_strip_raises() -> None:
    with pytest.raises(InvalidSlug):
        slugify("!!!")


def test_dated_slug_prepends_date() -> None:
    assert dated_slug("Add OAuth", date="2026-06-01") == "2026-06-01-add-oauth"


def test_dated_slug_caps_name_not_date() -> None:
    # The name keeps the MAX_SLUG_LEN cap; the 11-char date prefix is added on
    # top, so a long title never silently loses name chars to the prefix.
    s = dated_slug("x" * 200, date="2026-06-01")
    assert s == "2026-06-01-" + "x" * MAX_SLUG_LEN
    assert len(s) == 11 + MAX_SLUG_LEN


def test_dated_slug_empty_name_raises() -> None:
    with pytest.raises(InvalidSlug):
        dated_slug("!!!", date="2026-06-01")


def test_split_date_prefix_roundtrip() -> None:
    assert split_date_prefix("2026-06-01-add-oauth") == ("2026-06-01", "add-oauth")


def test_split_date_prefix_absent() -> None:
    assert split_date_prefix("add-oauth") == (None, "add-oauth")
