"""new_change_id: immutable Change identity YYYY-MM-DD-<6 ref-safe chars> (spec
§1, AC 2). Format asserted over many draws; uniqueness asserted at a small N to
avoid a latent birthday flake (secrets isn't seedable)."""

from __future__ import annotations

import re

from imp.engine.slug import new_change_id

_ID_RE = re.compile(r"^2026-06-04-[a-z0-9]{6}$")


def test_new_change_id_format() -> None:
    for _ in range(200):
        assert _ID_RE.fullmatch(new_change_id(date="2026-06-04"))


def test_new_change_id_unique_small_n() -> None:
    # ~100 draws: collision prob ~2e-6. Do NOT raise to 1000 (~2e-4 ≈ a flake
    # every ~4000 runs); secrets can't be seeded, so a large-N dedup can't be
    # made deterministic.
    ids = [new_change_id(date="2026-06-04") for _ in range(100)]
    assert len(set(ids)) == len(ids)


def test_new_change_id_uses_given_date() -> None:
    assert new_change_id(date="2025-01-09").startswith("2025-01-09-")
