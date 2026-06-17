"""Slug derivation (spec §3.1): lowercase, non-alphanumerics -> '-', collapse
repeats, trim. The id alone names the branch/worktree/state dir; the handle is a
display name + filename stem derived from title + id."""

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


# Immutable Change identity (spec AC 1): 6 chars from [a-z0-9] drawn with
# secrets, no date prefix. Ref-safe and coordination-free; collisions are caught
# by the branch/worktree-exists guard at create time, not avoided by a date.
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_change_id() -> str:
    """6 random ref-safe chars from [a-z0-9]."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))


def derive_handle(title: str, change_id: str) -> str:
    """`slugify(title)-<id>` once named; the bare `<id>` while unnamed (title
    blank or unsluggable). The id tail makes a handle self-identifying: seeing
    `add-oauth-abc123.md` names its change exactly."""
    try:
        return f"{slugify(title)}-{change_id}"
    except InvalidSlug:
        return change_id
