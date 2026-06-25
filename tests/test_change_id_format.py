"""new_change_id: dateless immutable identity, a 6-char pronounceable id — a
consonant/vowel/consonant/vowel stem plus two digits (spec AC 1). Format
asserted over many draws; uniqueness asserted at a small N — the keyspace is
only ~640k (16x5x16x5x100), so N is kept low to keep birthday-collision odds
negligible (secrets isn't seedable)."""

from __future__ import annotations

import re

from imp.engine.change_id import new_change_id

_ID_RE = re.compile(r"^[bdfghjklmnprstvz][aeiou][bdfghjklmnprstvz][aeiou][0-9]{2}$")


def test_new_change_id_format() -> None:
    for _ in range(200):
        assert _ID_RE.fullmatch(new_change_id())


def test_new_change_id_unique_small_n() -> None:
    ids = [new_change_id() for _ in range(20)]
    assert len(set(ids)) == len(ids)


def test_new_change_id_has_no_date_prefix() -> None:
    assert not re.match(r"^\d{4}-\d{2}-\d{2}", new_change_id())
