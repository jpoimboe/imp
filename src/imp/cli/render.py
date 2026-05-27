"""Human-readable renderer for `imp log` / `imp log --full`.

Self-contained: depends only on the event-dict shape used in
``events.jsonl``. No imports from ``imp.engine.*``. Pure functions and one
generator; no global state.

Spec: ``docs/superpowers/specs/2026-05-27-imp-log-readability-design.md``.
"""

from __future__ import annotations

import os
from typing import Any


_ANSI: dict[str, str] = {
    "dim":     "\x1b[2m",
    "bold":    "\x1b[1m",
    "red":     "\x1b[31m",
    "green":   "\x1b[32m",
    "yellow":  "\x1b[33m",
    "blue":    "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan":    "\x1b[36m",
    "gray":    "\x1b[90m",  # bright-black, not white
    "reset":   "\x1b[0m",
}


def _style(text: str, *codes: str, enabled: bool) -> str:
    """Wrap ``text`` in the named ANSI codes when ``enabled``.

    Returns ``text`` unchanged when disabled. Codes are applied in argument
    order; a single ``reset`` is appended once.
    """
    if not enabled or not codes:
        return text
    prefix = "".join(_ANSI[c] for c in codes)
    return f"{prefix}{text}{_ANSI['reset']}"


def should_use_color(stream: Any) -> bool:
    """Color is enabled iff ``stream.isatty()`` is true and ``NO_COLOR`` is
    unset in the environment. Matches the de-facto ``NO_COLOR`` convention.
    """
    return bool(stream.isatty()) and "NO_COLOR" not in os.environ


# ----------------------------------------------------------------------
# Per-event formatters. Each returns the summary text only (no timestamp,
# no type column, no indent). ``full`` only affects tool_call/tool_result;
# every other formatter ignores it.
# ----------------------------------------------------------------------

def _fmt_change_created(p: dict[str, Any], full: bool, color: bool) -> str:
    return f"#{p.get('change_id', '?')}  {p.get('title', '')}"


def _fmt_workflow_started(p: dict[str, Any], full: bool, color: bool) -> str:
    name = p.get("name", "?")
    frm = p.get("from_phase")
    return f"{name}  (from {frm})" if frm else str(name)


def _fmt_workflow_paused(p: dict[str, Any], full: bool, color: bool) -> str:
    reason = p.get("reason")
    return f"paused  ({reason})" if reason else "paused"


def _fmt_workflow_resumed(p: dict[str, Any], full: bool, color: bool) -> str:
    return "resumed"
