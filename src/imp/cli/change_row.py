from __future__ import annotations

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
