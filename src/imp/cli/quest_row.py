# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import shutil
from datetime import datetime
from enum import Enum
from typing import NamedTuple

from imp.cli import palette as pal

# Quoted-prompt prefix, shared so surfaces can't drift; callers apply dim.
PROMPT_PREFIX = "> "


class NameKind(Enum):
    """Which of the three display-name cases a resolved name is."""
    TITLE = "title"        # a real title: plain default foreground
    PROMPT = "prompt"      # titleless, falls back to the opening prompt: dim, `> `-prefixed
    UNNAMED = "unnamed"    # titleless with no prompt: the dim `(unnamed)` placeholder


class ResolvedName(NamedTuple):
    text: str          # flattened but UNtruncated; callers clip to their own width
    kind: NameKind


def resolve_display_name(title: str | None, intent: str | None) -> ResolvedName:
    """Resolve (title, intent) to display text plus a case signal for callers to style."""
    raw = (title or "").strip()
    if raw:
        return ResolvedName(raw, NameKind.TITLE)
    flat = " ".join(intent.split()) if intent else ""
    if flat:
        return ResolvedName(flat, NameKind.PROMPT)
    return ResolvedName("(unnamed)", NameKind.UNNAMED)


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
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "-"


ID_W = 6        # bare 6-char quest id
STATUS_W = 35   # merged status + " (step-label)"; preserves the old 13+2+20 budget
AGE_W = 7       # header "UPDATED" (7) aligns with the ≤4-char relative-age data
NAME_MIN = 10   # never shrink the trailing NAME column below this
NAME_CAP = 70   # cap a long name/intent's pull on the shared width so STATUS keeps its content

# Fixed columns plus their three 2-space gaps; the NAME column gets whatever
# terminal width remains after this.
_PREFIX_W = ID_W + STATUS_W + AGE_W + 6

# Meaning-based palette keyed by the resolved word: bold weight for
# waiting / error / merge_blocked; every other status renders plain.
STATUS_COLORS: dict[str, tuple[str, ...]] = {
    "waiting": ("bold",),
    "error": ("bold",),
    "merge_blocked": ("bold",),
}


def display_word(resolved: str) -> str:
    """Two words for `merge_blocked`; else identity (color/membership key on the underscore)."""
    return "merge blocked" if resolved == "merge_blocked" else resolved


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def name_budget(*, indent: int = 0) -> int:
    """Width available for the trailing NAME column on the current terminal.

    `indent` accounts for any prefix a caller prints before the row (e.g. the
    resume picker's selection marker). Falls back to an 80-col terminal when
    stdout isn't a tty (pipes, the remote/telegram path), keeping piped output
    stable, capped at NAME_CAP so wide terminals stay compact, and never below
    NAME_MIN.
    """
    cols = shutil.get_terminal_size((80, 24)).columns
    return max(NAME_MIN, min(NAME_CAP, cols - _PREFIX_W - indent))


def overview_widths(
    rows: list[tuple[str, str | None, str]],
    *,
    name_cap: int | None = None,
) -> tuple[int, int]:
    """Compute (status_width, name_width) for the overview from its rendered
    rows. Each row is (status_word, inner_label_or_None, name_text). STATUS and
    NAME share the flexible region left after the fixed ID/UPDATED columns and
    the three 2-space gaps; NAME is the first claimant (capped at NAME_CAP for
    sizing) and STATUS takes the leftover, floored at the status-word width and
    grown only up to the width its own content needs. When `name_cap` is not
    None, the final NAME width is clamped down to it (STATUS is not grown to
    absorb the freed space)."""
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
    if name_cap is not None:
        name_width = min(name_cap, name_width)
    return status_width, name_width


def header_line(*, color: bool, name_width: int,
                status_width: int = STATUS_W) -> str:
    text = (f"{'ID':<{ID_W}}  {'NAME':<{name_width}}  "
            f"{'STATUS':<{status_width}}  UPDATED")
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

    When the full ` (label)` suffix does not fit the cell — either no room for
    the framing (budget < 1) or the label itself is longer than the budget —
    the whole parenthetical is dropped and the bare status word is returned,
    never a partial `(…)`; the segment length is always <= status_width.
    """
    label = compose_status_label(step, step_label, phase_label)
    if label is None:
        return status
    budget = status_width - len(status) - 3   # one space + "(" + ")"
    if budget < 1 or len(label) > budget:
        return status
    return f"{status} ({label})"


def format_quest_line(*, quest_id: str, title: str, status: str, step: str,
                       step_label: str | None = None,
                       phase_label: str | None = None,
                       last_iso: str | None, now: datetime,
                       color: bool, name_width: int = 80,
                       status_width: int = STATUS_W, dim: bool = False,
                       intent: str | None = None) -> str:
    id_pad = f"{quest_id:<{ID_W}}"
    shown = display_word(status)
    visible = _status_segment(shown, step, step_label,
                              phase_label=phase_label, status_width=status_width)
    status_pad = f"{visible:<{status_width}}"
    age_text = relative_age(last_iso, now=now)   # final column, emitted bare
    # PROMPT fallback gets the `> ` prefix and dim, clipped to name_width.
    resolved = resolve_display_name(title, intent)
    if resolved.kind is NameKind.PROMPT:
        name_text = PROMPT_PREFIX + _truncate(resolved.text,
                                              name_width - len(PROMPT_PREFIX))
    else:
        name_text = _truncate(resolved.text, name_width)
    name_pad = " " * (name_width - len(name_text))   # right-pad NAME so STATUS aligns
    is_fallback = resolved.kind is not NameKind.TITLE
    if dim:
        line = f"{id_pad}  {name_text}{name_pad}  {status_pad}  {age_text}"
        return pal.ansi(line, "dim", enabled=color)
    name_cell = pal.ansi(name_text, "dim", enabled=color) if is_fallback else name_text
    codes = STATUS_COLORS.get(status, ())
    pad = " " * (status_width - len(visible))
    if color:
        suffix = visible[len(shown):]             # " (label)" or ""
        status_word = pal.ansi(shown, *codes, enabled=True) if codes else shown
        suffix_cell = pal.ansi(suffix, "dim", enabled=True) if suffix else ""
        status_cell = status_word + suffix_cell + pad
    else:
        status_cell = status_pad
    return f"{id_pad}  {name_cell}{name_pad}  {status_cell}  {age_text}"
