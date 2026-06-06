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
    "gray": "\x1b[90m", "bright_blue": "\x1b[94m", "step": "\x1b[36m",
}

# A CUSTOM low-saturation ACCENT palette — not a crib of any published theme, and
# (deliberately) no foreground/background here: imp never paints the prose fg or
# the terminal bg, it only paints these accents on top of whatever surface the
# terminal supplies. It's hand-tuned: the six accents all sit around ~40-50%
# saturation and ~70% value, and that uniform flatness (not any hue scheme — the
# hues are a normal 6-way terminal spread) is what makes it read as one calm, muted
# family. The neutral `gray` is deliberately dim so chrome (breadcrumbs / step +
# tool lines) recedes almost to nothing. We surveyed Selenized / Everforest /
# Kanagawa / Gruvbox / Zenburn / Catppuccin / Melange and none matched this look, so
# it stays bespoke. `light` re-tunes the same roles (darker, for legibility on a
# near-white surface). Emitted as 24-bit truecolor so the look is identical in every
# truecolor terminal (and in the snap-ui mockup). These apply only when the resolved
# theme is dark/light; the `ansi` theme above is the fallback for non-truecolor
# terminals.
_DARK_RGB: dict[str, tuple[int, int, int]] = {
    "red": (0xC0, 0x6A, 0x66), "green": (0x90, 0xA9, 0x59),
    "yellow": (0xC9, 0xA5, 0x54), "blue": (0x6E, 0x90, 0xB8),
    "magenta": (0xA8, 0x84, 0xB5), "cyan": (0x5E, 0xA0, 0x9E),
    "gray": (0x55, 0x57, 0x53), "bright_blue": (0x7F, 0xA2, 0xCA),
    # `step` is a darker cyan than `phase` — a step reads as a "mini-phase".
    "step": (0x4D, 0x8C, 0x8A),
}
_LIGHT_RGB: dict[str, tuple[int, int, int]] = {
    "red": (0xA8, 0x3B, 0x38), "green": (0x55, 0x6F, 0x22),
    "yellow": (0x8A, 0x6D, 0x12), "blue": (0x2F, 0x5C, 0x96),
    "magenta": (0x7A, 0x49, 0x96), "cyan": (0x2A, 0x73, 0x70),
    "gray": (0xA6, 0xAA, 0xA6), "bright_blue": (0x27, 0x5A, 0xA0),
    "step": (0x3A, 0x84, 0x80),   # darker-cyan step on the light surface too
}


# A REPRESENTATIVE terminal surface — (background, foreground) — for previewing
# each theme. imp never paints either: in a real run the terminal supplies the
# background and the prose foreground, and imp only paints accents on top. So these
# are NOT imp's colors; they exist purely so the snap-ui mockup (impty.render) draws
# imp's accents on a realistic terminal, the way you'd actually see them. `dark` is
# a common dark terminal (#1e1e1e on white text); `light` a common light one.
_SURFACES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "dark": ((0x1E, 0x1E, 0x1E), (0xFF, 0xFF, 0xFF)),
    "light": ((0xFA, 0xFA, 0xF8), (0x2A, 0x2A, 0x2A)),
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
    "step": ("step",),                 # a darker-cyan "mini-phase" label
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
