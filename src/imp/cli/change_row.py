from __future__ import annotations

import re
from datetime import datetime


def relative_age(then_iso: str | None, *, now: datetime) -> str:
    if not then_iso:
        return "-"
    try:
        then = datetime.fromisoformat(then_iso)
    except ValueError:
        return "-"
    secs = max(0, int((now - then).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


_MODERN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-([a-z0-9]{6})$")


def resume_token(change_id: str) -> str:
    """6-char suffix for a modern `YYYY-MM-DD-xxxxxx` id, or the full id for legacy slugs."""
    m = _MODERN_ID.match(change_id)
    return m.group(1) if m else change_id
