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


def format_timestamp(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "-"


ID_W = 6        # bare 6-char Change id
STATUS_W = 35   # merged status + " (step-label)"; preserves the old 13+2+20 budget
AGE_W = 7       # header "UPDATED" (7) aligns with the ≤4-char relative-age data
NAME_MIN = 10   # never shrink the trailing NAME column below this
NAME_CAP = 60   # cap a long name/intent's pull on the shared width so STATUS keeps its content

# Fixed columns plus their three 2-space gaps; the NAME column gets whatever
# terminal width remains after this.
_PREFIX_W = ID_W + STATUS_W + AGE_W + 6

STATUS_COLORS: dict[str, tuple[str, ...]] = {
    "running": ("green",),
    "merged": ("green",),
    "new": ("cyan",),
    "paused": ("cyan",),
    "merge_blocked": ("wordmark",),
    "draft": ("dim",),
    "backlog": ("dim",),
    "idea": ("dim",),
    "followup": ("dim",),
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


def overview_widths(
    rows: list[tuple[str, str | None, str]],
) -> tuple[int, int]:
    """Compute (status_width, name_width) for the overview from its rendered
    rows. Each row is (status_word, inner_label_or_None, name_text). STATUS and
    NAME share the flexible region left after the fixed ID/UPDATED columns and
    the three 2-space gaps; NAME is the first claimant (capped at NAME_CAP for
    sizing) and STATUS takes the leftover, floored at the status-word width and
    grown only up to the width its own content needs."""
    cols = shutil.get_terminal_size((80, 24)).columns
    region = cols - (ID_W + AGE_W + 6)     # three 2-space gaps

    def cell_len(status: str, label: str | None) -> int:
        return len(status) if label is None else len(status) + len(label) + 3

    status_floor = max([len("STATUS")] + [len(s) for s, _, _ in rows])
    status_content = max([len("STATUS")] + [cell_len(s, lbl) for s, lbl, _ in rows])
    name_content = min(NAME_CAP, max([0] + [len(n) for _, _, n in rows]))

    status_width = min(max(region - name_content, status_floor), status_content)
    status_width = min(status_width, max(status_floor, region - NAME_MIN))
    name_width = max(NAME_MIN, region - status_width)
    return status_width, name_width


def header_line(*, color: bool, status_width: int = STATUS_W) -> str:
    text = (f"{'ID':<{ID_W}}  {'STATUS':<{status_width}}  "
            f"{'UPDATED':<{AGE_W}}  NAME")
    return pal.ansi(text, "dim", enabled=color)


def compose_status_label(step: str, step_label: str | None,
                         phase_label: str | None) -> str | None:
    """The inner STATUS label: `phase_label · step` when a phase label is
    present, else the step label (or the raw step name). None for a stepless
    row so the caller renders the bare status word."""
    if not step or step == "-":
        return None
    label = step_label or step
    return f"{phase_label} · {label}" if phase_label else label


def _status_segment(status: str, step: str, step_label: str | None,
                    *, phase_label: str | None = None,
                    status_width: int = STATUS_W) -> str:
    """Status word plus dim ` (label)` suffix; bare word for stepless rows.

    When the cell is squeezed so tight there is no room for a ` (…)` suffix
    (budget < 1), the suffix is dropped and the bare status word is returned,
    so the segment length is always <= status_width and the cell never spills.
    """
    label = compose_status_label(step, step_label, phase_label)
    if label is None:
        return status
    budget = status_width - len(status) - 3   # one space + "(" + ")"
    if budget < 1:
        return status
    if len(label) > budget:
        label = _truncate(label, budget)
    return f"{status} ({label})"


def format_change_line(*, quest_id: str, title: str, status: str, step: str,
                       step_label: str | None = None,
                       phase_label: str | None = None,
                       last_iso: str | None, now: datetime,
                       color: bool, name_width: int = 80,
                       status_width: int = STATUS_W, dim: bool = False,
                       intent: str | None = None) -> str:
    raw = title.strip()
    id_pad = f"{quest_id:<{ID_W}}"
    visible = _status_segment(status, step, step_label,
                              phase_label=phase_label, status_width=status_width)
    status_pad = f"{visible:<{status_width}}"
    age_pad = f"{relative_age(last_iso, now=now):<{AGE_W}}"
    # NAME is the final column, so it needs no trailing pad — only a cap so a
    # very long name can't spill past the terminal's right edge.
    if not raw and intent:
        # The intent fallback is the verbatim opening prompt, which can be
        # multi-line (e.g. a pasted traceback). Flatten internal whitespace to
        # single spaces so the NAME cell stays on one row before truncating.
        name_text = _truncate(" ".join(intent.split()), name_width)
    else:
        name_text = "(unnamed)" if not raw else _truncate(raw, name_width)
    if dim:
        line = f"{id_pad}  {status_pad}  {age_pad}  {name_text}"
        return pal.ansi(line, "dim", enabled=color)
    name_cell = pal.ansi(name_text, "dim", enabled=color) if not raw else name_text
    codes = STATUS_COLORS.get(status, ())
    pad = " " * (status_width - len(visible))
    if color:
        suffix = visible[len(status):]            # " (label)" or ""
        status_word = pal.ansi(status, *codes, enabled=True) if codes else status
        suffix_cell = pal.ansi(suffix, "dim", enabled=True) if suffix else ""
        status_cell = status_word + suffix_cell + pad
    else:
        status_cell = status_pad
    return f"{id_pad}  {status_cell}  {age_pad}  {name_cell}"
