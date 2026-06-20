"""new_change_id: dateless immutable identity, 6 chars from [a-z0-9] (spec AC 1).
Format asserted over many draws; uniqueness asserted at a small N to avoid a
latent birthday flake (secrets isn't seedable)."""

from __future__ import annotations

import re

from imp.engine.change_id import new_change_id

_ID_RE = re.compile(r"^[a-z0-9]{6}$")


def test_new_change_id_format() -> None:
    for _ in range(200):
        assert _ID_RE.fullmatch(new_change_id())


def test_new_change_id_unique_small_n() -> None:
    ids = [new_change_id() for _ in range(100)]
    assert len(set(ids)) == len(ids)


def test_new_change_id_has_no_date_prefix() -> None:
    assert not re.match(r"^\d{4}-\d{2}-\d{2}", new_change_id())
