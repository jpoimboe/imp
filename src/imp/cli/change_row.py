from __future__ import annotations

import shutil
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


ID_W = 6        # bare 6-char Change id
STATUS_W = 13   # widest status: len("merge_blocked")
STEP_W = 20     # widest default-workflow step name
AGE_W = 4
NAME_MIN = 10   # never shrink the trailing NAME column below this

# Fixed columns plus their four 2-space gaps; the NAME column gets whatever
# terminal width remains after this.
_PREFIX_W = ID_W + STATUS_W + STEP_W + AGE_W + 8

STATUS_COLORS: dict[str, tuple[str, ...]] = {
    "running": ("green",),
    "merged": ("green",),
    "paused": ("cyan",),
    "merge_blocked": ("yellow",),
    "errored": ("bold", "red"),
    "draft": ("magenta",),
    "backlog": ("dim",),
}


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def name_budget(*, indent: int = 0) -> int:
    """Width available for the trailing NAME column on the current terminal.

    `indent` accounts for any prefix a caller prints before the row (e.g. the
    resume picker's selection marker). Falls back to an 80-col terminal when
    stdout isn't a tty (pipes, the remote/telegram path), keeping piped output
    stable, and never shrinks below NAME_MIN.
    """
    cols = shutil.get_terminal_size((80, 24)).columns
    return max(NAME_MIN, cols - _PREFIX_W - indent)


def header_line(*, color: bool) -> str:
    text = (f"{'ID':<{ID_W}}  {'STATUS':<{STATUS_W}}  "
            f"{'STEP':<{STEP_W}}  {'AGE':<{AGE_W}}  NAME")
    return pal.ansi(text, "dim", enabled=color)


def format_change_line(*, change_id: str, title: str, status: str, step: str,
                       last_iso: str | None, now: datetime,
                       color: bool, name_width: int = 80, dim: bool = False) -> str:
    raw = title.strip()
    id_pad = f"{change_id:<{ID_W}}"
    status_pad = f"{status:<{STATUS_W}}"
    step_pad = f"{_truncate(step or '-', STEP_W):<{STEP_W}}"
    age_pad = f"{relative_age(last_iso, now=now):<{AGE_W}}"
    # NAME is the final column, so it needs no trailing pad — only a cap so a
    # very long name can't spill past the terminal's right edge.
    name_text = "(unnamed)" if not raw else _truncate(raw, name_width)
    if dim:
        line = f"{id_pad}  {status_pad}  {step_pad}  {age_pad}  {name_text}"
        return pal.ansi(line, "dim", enabled=color)
    name_cell = pal.ansi(name_text, "dim", enabled=color) if not raw else name_text
    codes = STATUS_COLORS.get(status, ())
    if color and codes:
        status_cell = pal.ansi(status, *codes, enabled=True) + " " * (STATUS_W - len(status))
    else:
        status_cell = status_pad
    return f"{id_pad}  {status_cell}  {step_pad}  {age_pad}  {name_cell}"
