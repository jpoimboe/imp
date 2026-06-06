# src/imp/cli/palette.py
"""Shared color palette + accent map + fold glyphs for both renderers.

Ported from the deleted render.py (§9 keeps today's palette unchanged). No
third-party imports — consumed by render_text.py (imp log) and live_driver.py
(the live stream), both of which emit raw ANSI via ansi()."""
from __future__ import annotations

import os
from typing import Any

# Weight/reset are theme-independent (not hue), so they live outside the themes.
_ATTRS: dict[str, str] = {"dim": "\x1b[2m", "bold": "\x1b[1m", "reset": "\x1b[0m"}

# The `ansi` theme: the 16 named SGR colors. The terminal paints these from its
# OWN palette, so they adapt to any background — the safe, universal default and
# the only theme that's correct without knowing the terminal's background.
_ANSI_NAMED: dict[str, str] = {
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m",
    "gray": "\x1b[90m", "bright_blue": "\x1b[94m",
}

# The Selenized palette (https://github.com/jan-warchol/selenized) — a cohesive,
# accessibility-tuned scheme with matched variants. We use its published RGB
# values verbatim (not a hand-tuned lookalike): the six accents from the normal
# set, `gray` from `dim_0` (Selenized's de-emphasis color, for breadcrumbs /
# chrome), and `bright_blue` from `br_blue` (the user's turn). `dark` uses the
# `selenized black` variant — a NEUTRAL near-black surface, not the tinted teal of
# `selenized dark`, so the dim chrome stays legible (its gray isn't lost in a
# same-hue background) and the surface reads as black, not blue. `light` uses
# `selenized light`. Emitted as 24-bit truecolor so the look is identical in every
# truecolor terminal (and in the snap-ui mockup, which renders whatever imp emits).
# These apply only when the resolved theme is dark/light; the `ansi` theme above is
# the fallback for non-truecolor terminals.
_DARK_RGB: dict[str, tuple[int, int, int]] = {  # selenized black
    "red": (0xED, 0x4A, 0x46), "green": (0x70, 0xB4, 0x33),
    "yellow": (0xDB, 0xB3, 0x2D), "blue": (0x36, 0x8A, 0xEB),
    "magenta": (0xEB, 0x6E, 0xB7), "cyan": (0x3F, 0xC5, 0xB7),
    "gray": (0x77, 0x77, 0x77), "bright_blue": (0x4F, 0x9C, 0xFE),
}
_LIGHT_RGB: dict[str, tuple[int, int, int]] = {  # selenized light
    "red": (0xD2, 0x21, 0x2D), "green": (0x48, 0x91, 0x00),
    "yellow": (0xAD, 0x89, 0x00), "blue": (0x00, 0x72, 0xD4),
    "magenta": (0xCA, 0x48, 0x98), "cyan": (0x00, 0x9C, 0x8F),
    "gray": (0x90, 0x99, 0x95), "bright_blue": (0x00, 0x6D, 0xCE),
}


# The surface each truecolor theme is designed for: (background, foreground). imp
# never paints a background — these are the single source of truth for the theme's
# intended surface, consumed by the snap-ui mockup (impty.render) so a rendered PNG
# matches a real terminal on that theme. Selenized bg_0 / fg_0 per variant
# (dark = `selenized black` — neutral near-black; light = `selenized light`).
_SURFACES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "dark": ((0x18, 0x18, 0x18), (0xB9, 0xB9, 0xB9)),
    "light": ((0xFB, 0xF3, 0xDB), (0x53, 0x67, 0x6D)),
}


def theme_rgb(theme: str) -> dict[str, tuple[int, int, int]]:
    """The accent name→RGB map for a truecolor theme. `ansi` has no canonical RGB
    (the terminal paints it), so the dark map stands in when a mockup needs one."""
    return {"dark": _DARK_RGB, "light": _LIGHT_RGB}.get(theme, _DARK_RGB)


def surface(theme: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The (background, foreground) the theme is designed for (see _SURFACES)."""
    return _SURFACES.get(theme, _SURFACES["dark"])


def _truecolor_map(rgb: dict[str, tuple[int, int, int]]) -> dict[str, str]:
    return {name: f"\x1b[38;2;{r};{g};{b}m" for name, (r, g, b) in rgb.items()}


# Theme name → escape map (color name → SGR sequence).
THEMES: tuple[str, ...] = ("dark", "light", "ansi")
_THEMES: dict[str, dict[str, str]] = {
    "ansi": {**_ATTRS, **_ANSI_NAMED},
    "dark": {**_ATTRS, **_truecolor_map(_DARK_RGB)},
    "light": {**_ATTRS, **_truecolor_map(_LIGHT_RGB)},
}

# The ACTIVE escape map, consulted by ansi(). Defaults to `ansi` — universal,
# and what the golden/SGR-literal tests expect. The CLI calls set_theme() once
# at startup from the resolved config; the snap-ui mockup sets `dark`.
_active_theme = "ansi"
_ANSI: dict[str, str] = dict(_THEMES["ansi"])


def set_theme(name: str) -> None:
    """Swap the active color theme. An unknown name falls back to `ansi`."""
    global _ANSI, _active_theme
    _active_theme = name if name in _THEMES else "ansi"
    _ANSI = dict(_THEMES[_active_theme])


def active_theme() -> str:
    return _active_theme


def ansi(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    prefix = "".join(_ANSI[c] for c in codes)
    return f"{prefix}{text}{_ANSI['reset']}"


# The interactive input field (spec §3). The caret marks the user's turn; the
# rules bracket it so the field is unmistakably input, not agent output — a
# quiet replacement for the old bright-blue background fill. bright_blue keeps
# the "user = blue" identity (NODE_COLORS["user"]); rules stay gray so they
# recede like other navigational chrome.
CARET = "> "


def caret(*, enabled: bool) -> str:
    """The styled prompt caret as a raw ANSI string (callers that drive
    prompt_toolkit must wrap it in ANSI(); plain-stdout callers print it)."""
    return ansi(CARET, "bright_blue", enabled=enabled)


def rule(width: int, *, enabled: bool) -> str:
    """A horizontal rule the width of the terminal, gray so it recedes."""
    return ansi("─" * max(1, width), "gray", enabled=enabled)


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
    "breadcrumb": ("gray",),           # per-step completion line — recede entirely
    "gate": ("yellow",),               # semantic
    "tools": ("gray",),                # lean one-line recap + --full per-call dump
    "subagent": ("magenta",),          # header + gutter + outcome; never bold
    "thinking": ("dim",),              # --full only
    "metadata": ("dim",),              # --full only
    "error": ("bold", "red"),          # semantic
    "warning": ("yellow",),            # semantic
    "user": ("bright_blue",),          # bright-blue caret/text marks the user's turn
    "agent": (),                       # calm baseline — plain default fg; eye rests
}

_FOLD_UNICODE = {"collapsed": "▸", "expanded": "▾"}
_FOLD_ASCII = {"collapsed": ">", "expanded": "v"}


def fold_glyph(*, expanded: bool, unicode: bool = True) -> str:
    table = _FOLD_UNICODE if unicode else _FOLD_ASCII
    return table["expanded" if expanded else "collapsed"]
