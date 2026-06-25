"""Immutable Change identity (spec AC 1): a 6-char pronounceable id — a
consonant, vowel, consonant, vowel, then two digits (e.g. kobe47) — drawn with
secrets. Ref-safe, coordination-free, no date prefix. The CVCV stem has no
consonant clusters, so it stays pronounceable. Collisions are caught by the
branch/worktree-exists guard at create time, not avoided by a date."""

from __future__ import annotations

import secrets

_CONSONANTS = "bdfghjklmnprstvz"
_VOWELS = "aeiou"
_DIGITS = "0123456789"


def new_change_id() -> str:
    """6 ref-safe chars: consonant, vowel, consonant, vowel, digit, digit."""
    return (
        secrets.choice(_CONSONANTS)
        + secrets.choice(_VOWELS)
        + secrets.choice(_CONSONANTS)
        + secrets.choice(_VOWELS)
        + secrets.choice(_DIGITS)
        + secrets.choice(_DIGITS)
    )
