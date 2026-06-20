"""Immutable Change identity (spec AC 1): a 6-char id drawn from [a-z0-9] with
secrets — ref-safe, coordination-free, no date prefix. Collisions are caught by
the branch/worktree-exists guard at create time, not avoided by a date."""

from __future__ import annotations

import secrets

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_change_id() -> str:
    """6 random ref-safe chars from [a-z0-9]."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
