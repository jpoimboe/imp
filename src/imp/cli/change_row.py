from __future__ import annotations

import re
from datetime import datetime

from imp.cli import palette as pal


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


TITLE_W = 24
STATUS_W = 13   # widest status: len("merge_blocked")
STEP_W = 20     # widest default-workflow step name
AGE_W = 4

_STATUS_COLORS: dict[str, tuple[str, ...]] = {
    "running": ("green",),
    "paused": ("cyan",),
    "merge_blocked": ("yellow",),
    "errored": ("bold", "red"),
}


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def header_line(*, color: bool) -> str:
    text = (f"{'TITLE':<{TITLE_W}}  {'STATUS':<{STATUS_W}}  "
            f"{'STEP':<{STEP_W}}  {'AGE':<{AGE_W}}  ID")
    return pal.ansi(text, "dim", enabled=color)


def format_change_line(*, change_id: str, title: str, status: str, step: str,
                       last_iso: str | None, now: datetime,
                       color: bool, dim: bool = False) -> str:
    raw = title.strip()
    title_text = "(unnamed)" if not raw else _truncate(raw, TITLE_W)
    title_pad = f"{title_text:<{TITLE_W}}"
    status_pad = f"{status:<{STATUS_W}}"
    step_pad = f"{(step or '-'):<{STEP_W}}"
    age_pad = f"{relative_age(last_iso, now=now):<{AGE_W}}"
    token = resume_token(change_id)
    if dim:
        line = f"{title_pad}  {status_pad}  {step_pad}  {age_pad}  {token}"
        return pal.ansi(line, "dim", enabled=color)
    title_cell = pal.ansi(title_pad, "dim", enabled=color) if not raw else title_pad
    codes = _STATUS_COLORS.get(status, ())
    if color and codes:
        status_cell = pal.ansi(status, *codes, enabled=True) + " " * (STATUS_W - len(status))
    else:
        status_cell = status_pad
    return f"{title_cell}  {status_cell}  {step_pad}  {age_pad}  {token}"
