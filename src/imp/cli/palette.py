# src/imp/cli/palette.py
"""Shared color palette + accent map + fold glyphs for both renderers.

Ported from the deleted render.py (§9 keeps today's palette unchanged). No
third-party imports — consumed by render_text.py (raw ANSI) and tui.py (which
maps these color names onto its own rich/prompt_toolkit styles)."""
from __future__ import annotations

import os
from typing import Any

_ANSI: dict[str, str] = {
    "dim": "\x1b[2m", "bold": "\x1b[1m",
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m",
    "gray": "\x1b[90m", "reset": "\x1b[0m",
}


def ansi(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    prefix = "".join(_ANSI[c] for c in codes)
    return f"{prefix}{text}{_ANSI['reset']}"


def should_use_color(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ


# §9 accent: the ONLY action values the stream emits are continue/retry/pause/
# goto/unknown (executor._action_dict) plus the step_completed strings. Dead
# keys (next/loop/exhausted) are intentionally omitted — they never fire.
_ACCENT: dict[str, str] = {
    "continue": "green", "retry": "yellow", "pause": "cyan", "goto": "cyan",
}


def _action_kind(action: Any) -> str:
    if isinstance(action, dict):
        return str(action.get("kind", "unknown"))
    return str(action)


def accent_color(action: Any) -> str | None:
    return _ACCENT.get(_action_kind(action))


def action_text(action: Any) -> str:
    if isinstance(action, dict):
        kind = str(action.get("kind", "unknown"))
        if kind == "goto" and action.get("target"):
            return f"goto({action['target']})"
        return kind
    return str(action)


# §9 node-category → color names. Both views read these so colors stay in sync.
NODE_COLORS: dict[str, tuple[str, ...]] = {
    "workflow": ("bold", "magenta"),
    "phase": ("bold", "blue"),
    "step": ("cyan",),
    "gate": ("yellow",),
    "tools": ("gray",),
    "subagent": ("magenta",),
    "thinking": ("dim",),
    "metadata": ("dim",),
    "error": ("bold", "red"),
    "warning": ("yellow",),
    "user": ("blue",),
    "agent": ("bold",),            # LLM prose emphasis (was colorless)
}

_FOLD_UNICODE = {"collapsed": "▸", "expanded": "▾"}
_FOLD_ASCII = {"collapsed": ">", "expanded": "v"}


def fold_glyph(*, expanded: bool, unicode: bool = True) -> str:
    table = _FOLD_UNICODE if unicode else _FOLD_ASCII
    return table["expanded" if expanded else "collapsed"]
