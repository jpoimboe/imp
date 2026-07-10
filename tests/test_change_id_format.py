"""new_change_id: dateless immutable identity, a 6-char pronounceable id — a
consonant/vowel/consonant/vowel stem plus two digits (spec AC 1). Format
asserted over many draws; uniqueness asserted at a small N — the keyspace is
only ~640k (16x5x16x5x100), so N is kept low to keep birthday-collision odds
negligible (secrets isn't seedable)."""

from __future__ import annotations

import re

from imp.engine.change_id import (
    _CONSONANTS,
    _DENYLIST,
    _VOWELS,
    _is_clean_stem,
    new_change_id,
)

_ID_RE = re.compile(r"^[bdfghjklmnprstvz][aeiou][bdfghjklmnprstvz][aeiou][0-9]{2}$")


def test_new_change_id_format() -> None:
    for _ in range(200):
        assert _ID_RE.fullmatch(new_change_id())


def test_new_change_id_unique_small_n() -> None:
    ids = [new_change_id() for _ in range(20)]
    assert len(set(ids)) == len(ids)


def test_new_change_id_has_no_date_prefix() -> None:
    assert not re.match(r"^\d{4}-\d{2}-\d{2}", new_change_id())


def test_screening_rejects_every_denylisted_stem() -> None:
    assert _DENYLIST  # guard: an empty denylist would make this test vacuous
    for stem in _DENYLIST:
        assert _is_clean_stem(stem) is False


def test_screening_accepts_a_clean_stem() -> None:
    assert _is_clean_stem("kobe") is True


def test_new_change_id_never_emits_a_denylisted_stem() -> None:
    for _ in range(5000):
        assert new_change_id()[:4] not in _DENYLIST


def _all_clean_stems() -> list[str]:
    return [
        c1 + v1 + c2 + v2
        for c1 in _CONSONANTS
        for v1 in _VOWELS
        for c2 in _CONSONANTS
        for v2 in _VOWELS
        if _is_clean_stem(c1 + v1 + c2 + v2)
    ]


def test_new_change_id_avoids_existing_stems() -> None:
    # AC 1: over many draws with a small avoid-set, the returned stem is never
    # one of the avoided stems. |S|=3 out of ~6395 clean stems: a build that
    # ignored existing_stems would collide ~9 times in expectation over 20000
    # draws (P(catch) > 0.9999), so this test is falsifiable.
    avoid = frozenset({"kobe", "mira", "tavo"})
    assert avoid <= set(_all_clean_stems())  # guard: the avoided stems are clean
    for _ in range(20000):
        assert new_change_id(existing_stems=avoid)[:4] not in avoid


def test_new_change_id_with_avoid_set_keeps_format_and_denylist() -> None:
    # AC 2: for any S, the id is still CVCV + two digits and never denylisted.
    avoid = frozenset({"kobe", "mira"})
    for _ in range(2000):
        cid = new_change_id(existing_stems=avoid)
        assert _ID_RE.fullmatch(cid)
        assert cid[:4] not in _DENYLIST


def test_new_change_id_terminates_when_avoid_set_is_saturated() -> None:
    # AC 3: S contains every clean stem. The mint must still terminate and
    # return a valid-format, denylist-clean id (the last clean stem, a duplicate).
    saturated = frozenset(_all_clean_stems())
    cid = new_change_id(existing_stems=saturated)
    assert _ID_RE.fullmatch(cid)
    assert cid[:4] not in _DENYLIST
