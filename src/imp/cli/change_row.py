from __future__ import annotations

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


ID_W = 6        # bare 6-char Change id; the "ID" header also fits in 6 cols
TITLE_W = 24
STATUS_W = 13   # widest status: len("merge_blocked")
STEP_W = 20     # widest default-workflow step name
AGE_W = 4

STATUS_COLORS: dict[str, tuple[str, ...]] = {
    "running": ("green",),
    "merged": ("green",),
    "paused": ("cyan",),
    "merge_blocked": ("yellow",),
    "errored": ("bold", "red"),
    "draft": ("magenta",),
}


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def header_line(*, color: bool) -> str:
    text = (f"{'ID':<{ID_W}}  {'TITLE':<{TITLE_W}}  {'STATUS':<{STATUS_W}}  "
            f"{'STEP':<{STEP_W}}  {'AGE':<{AGE_W}}")
    return pal.ansi(text, "dim", enabled=color)


def format_change_line(*, change_id: str, title: str, status: str, step: str,
                       last_iso: str | None, now: datetime,
                       color: bool, dim: bool = False) -> str:
    raw = title.strip()
    id_pad = f"{change_id:<{ID_W}}"
    title_text = "(unnamed)" if not raw else _truncate(raw, TITLE_W)
    title_pad = f"{title_text:<{TITLE_W}}"
    status_pad = f"{status:<{STATUS_W}}"
    step_pad = f"{(step or '-'):<{STEP_W}}"
    age_pad = f"{relative_age(last_iso, now=now):<{AGE_W}}"
    if dim:
        line = f"{id_pad}  {title_pad}  {status_pad}  {step_pad}  {age_pad}"
        return pal.ansi(line, "dim", enabled=color)
    title_cell = pal.ansi(title_pad, "dim", enabled=color) if not raw else title_pad
    codes = STATUS_COLORS.get(status, ())
    if color and codes:
        status_cell = pal.ansi(status, *codes, enabled=True) + " " * (STATUS_W - len(status))
    else:
        status_cell = status_pad
    return f"{id_pad}  {title_cell}  {status_cell}  {step_pad}  {age_pad}"
