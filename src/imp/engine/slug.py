"""Slug derivation (spec §3.1): lowercase, non-alphanumerics -> '-', collapse
repeats, trim. One name for Change <-> branch <-> worktree <-> state dir."""

from __future__ import annotations

import re
import secrets

# Cap the slug length. The slug becomes a git branch (`imp/<slug>`), so an
# unbounded slug from a long title produces a ref path that overflows the
# filesystem's 255-byte limit ("cannot lock ref ... File name too long") and
# breaks worktree creation. 60 leaves ample room for the `imp/` prefix, the
# `.lock` suffix, and the surrounding worktree/state paths.
MAX_SLUG_LEN = 60


class InvalidSlug(ValueError):
    """Raised when a title slugifies to the empty string."""


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        raise InvalidSlug(f"{text!r} has no slug-able characters")
    if len(s) > MAX_SLUG_LEN:
        head = s[:MAX_SLUG_LEN]
        # Cut at the last word boundary within the cap so we don't split a
        # token; fall back to a hard truncate for a single oversized token.
        cut = head.rfind("-")
        s = (head[:cut] if cut > 0 else head).rstrip("-")
    return s


def dated_slug(name: str, *, date: str) -> str:
    """`<date>-<slugified name>`. `date` is a fixed-width `YYYY-MM-DD` string.
    The name keeps the MAX_SLUG_LEN cap (slugify), so the full slug is at most
    11 + MAX_SLUG_LEN chars — the date prefix does not eat the name budget.

    Returns `""` when `name` is blank or has no slug-able characters: an unnamed
    Change has an empty title, and `""` is the canonical "unnamed" signal
    (Change.slug derives from this). slugify itself still raises on empty — it is
    the validator rename_change uses for a *new* name."""
    try:
        return f"{date}-{slugify(name)}"
    except InvalidSlug:
        return ""


# Immutable Change identity (spec §1): YYYY-MM-DD- + 6 chars from [a-z0-9] drawn
# with secrets. Ref-safe, coordination-free, collision-free at this scale
# (~2.2 billion/day). The date prefix gives ULID-style chronological sort of the
# stable key; the suffix is unique within a day, not globally.
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_change_id(*, date: str) -> str:
    """`<date>-<6 random ref-safe chars>`. `date` is a fixed-width YYYY-MM-DD
    string (the caller passes today's date)."""
    suffix = "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
    return f"{date}-{suffix}"
