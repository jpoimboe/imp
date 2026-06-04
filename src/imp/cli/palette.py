# src/imp/cli/palette.py
"""Shared color palette + accent map + fold glyphs for both renderers.

Ported from the deleted render.py (§9 keeps today's palette unchanged). No
third-party imports — consumed by render_text.py (imp log) and live_driver.py
(the live stream), both of which emit raw ANSI via ansi()."""
from __future__ import annotations

import os
from typing import Any

_ANSI: dict[str, str] = {
    "dim": "\x1b[2m", "bold": "\x1b[1m",
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m",
    "gray": "\x1b[90m", "bg_bright_blue": "\x1b[104m", "reset": "\x1b[0m",
}


def ansi(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    prefix = "".join(_ANSI[c] for c in codes)
    return f"{prefix}{text}{_ANSI['reset']}"


def should_use_color(stream: Any) -> bool:
    # Precedence: NO_COLOR (disable) > force vars > isatty. NO_COLOR always
    # wins. CLICOLOR_FORCE (ripgrep/fd/bat) and FORCE_COLOR (npm/pytest) let
    # callers force color through a pipe, e.g. `imp log | less -R` or CI logs.
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("CLICOLOR_FORCE") or os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


# §9 accent: the ONLY action verbs the stream emits are continue/pause/
# goto/unknown (executor._action_payload). Dead keys (retry/next/loop/exhausted)
# are intentionally omitted — they never fire in the unified-step model.
_ACCENT: dict[str, str] = {
    "continue": "green", "retry": "yellow", "pause": "cyan", "goto": "cyan",
}


def _action_verb(action: Any) -> str:
    if isinstance(action, dict):
        return str(action.get("verb", "unknown"))
    return str(action)


def accent_color(action: Any) -> str | None:
    return _ACCENT.get(_action_verb(action))


def action_text(action: Any) -> str:
    if isinstance(action, dict):
        verb = str(action.get("verb", "unknown"))
        if verb == "goto" and action.get("target"):
            text = f"goto({action['target']})"
            if action.get("resume"):
                text += " [resume]"
            return text
        return verb
    return str(action)


# §9 node-category → color names. Both views read these so colors stay in sync.
# Ink spent backwards from frequency: the most frequent content (agent prose) is
# calmest; weight + color are reserved for speaker identity and structure.
NODE_COLORS: dict[str, tuple[str, ...]] = {
    "workflow": ("bold", "magenta"),   # appears once at top — the lone bold anchor
    "phase": ("cyan",),                # recedes: muted, no bold
    "step": ("gray",),                 # navigational chrome, not speech
    "gate": ("yellow",),               # semantic
    "tools": ("gray",),                # --full only
    "subagent": ("magenta",),          # header + gutter + outcome; never bold
    "thinking": ("dim",),              # --full only
    "metadata": ("dim",),              # --full only
    "error": ("bold", "red"),          # semantic
    "warning": ("yellow",),            # semantic
    "user": ("bg_bright_blue",),       # bright-blue bg paints the input region
    "agent": (),                       # calm baseline — plain default fg; eye rests
}

_FOLD_UNICODE = {"collapsed": "▸", "expanded": "▾"}
_FOLD_ASCII = {"collapsed": ">", "expanded": "v"}


def fold_glyph(*, expanded: bool, unicode: bool = True) -> str:
    table = _FOLD_UNICODE if unicode else _FOLD_ASCII
    return table["expanded" if expanded else "collapsed"]
