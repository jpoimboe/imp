import pytest

from imp.engine.slug import InvalidSlug, slugify


def test_basic() -> None:
    assert slugify("Add OAuth support") == "add-oauth-support"


def test_collapses_and_trims() -> None:
    assert slugify("  Foo / Bar!! ") == "foo-bar"
    assert slugify("a---b") == "a-b"


def test_three_word_phrase() -> None:
    assert slugify("brave silver otter") == "brave-silver-otter"


def test_empty_after_strip_raises() -> None:
    with pytest.raises(InvalidSlug):
        slugify("!!!")
