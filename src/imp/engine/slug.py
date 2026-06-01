"""Slug derivation (spec §3.1): lowercase, non-alphanumerics -> '-', collapse
repeats, trim. One name for Change <-> branch <-> worktree <-> state dir."""

from __future__ import annotations

import re

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
