"""Immutable Change identity (spec AC 1): a 6-char pronounceable id — a
consonant, vowel, consonant, vowel, then two digits (e.g. kobe47) — drawn with
secrets. Ref-safe, coordination-free, no date prefix. The CVCV stem has no
consonant clusters, so it stays pronounceable; an unfortunate-stem denylist is
resampled past. Collisions are caught by the branch/worktree-exists guard at
create time, not avoided by a date."""

from __future__ import annotations

import secrets

_CONSONANTS = "bdfghjklmnprstvz"
_VOWELS = "aeiou"
_DIGITS = "0123456789"

# CVCV stems that read as crude or offensive words; resampled past so no id
# surfaces one. All four letters are drawn from _CONSONANTS / _VOWELS.
_DENYLIST = frozenset({"puta", "kaka", "homo", "dodo", "nazi"})


def _is_clean_stem(stem: str) -> bool:
    """True when the four-letter CVCV stem is not in the denylist."""
    return stem not in _DENYLIST


_STEM_DRAWS = 100  # bounded resamples for a distinct stem; saturation is impossible in practice


def _draw_stem() -> str:
    return (
        secrets.choice(_CONSONANTS)
        + secrets.choice(_VOWELS)
        + secrets.choice(_CONSONANTS)
        + secrets.choice(_VOWELS)
    )


def _draw_clean_stem() -> str:
    """A denylist-clean CVCV stem (resamples past the denylist)."""
    while True:
        stem = _draw_stem()
        if _is_clean_stem(stem):
            return stem


def new_change_id(existing_stems: frozenset[str] = frozenset()) -> str:
    """6 ref-safe chars: consonant, vowel, consonant, vowel, digit, digit.

    Resamples the stem past `existing_stems` (bounded to `_STEM_DRAWS` tries);
    if every try collides (only reachable with a saturated set — impossible in
    practice) it accepts the last clean stem, so termination is guaranteed. The
    default empty set reproduces the prior no-argument behavior.
    """
    stem = _draw_clean_stem()
    for _ in range(_STEM_DRAWS):
        if stem not in existing_stems:
            break
        stem = _draw_clean_stem()
    return stem + secrets.choice(_DIGITS) + secrets.choice(_DIGITS)
