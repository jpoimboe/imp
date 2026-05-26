"""Slash command dispatcher. V0 only ships /help."""

from __future__ import annotations

from collections.abc import Callable


def is_slash_command(text: str) -> bool:
    return text.lstrip().startswith("/")


def run_slash_command(text: str, *, output_fn: Callable[[str], None]) -> None:
    cmd = text.lstrip().lstrip("/").split()[0] if text.strip() else ""
    if cmd == "help":
        _help(output_fn)
        return
    output_fn(f"unknown command: /{cmd}")
    _help(output_fn)


def _help(out: Callable[[str], None]) -> None:
    out("Available slash commands:")
    out("  /help              Show this message")
    out("")
    out("Keybindings:")
    out("  ESC                Re-prompt without sending (interactive steps)")
    out("  Ctrl+C / Ctrl+D    Pause and exit; resume with `imp resume <id>`")
    out("  Up-arrow           Recall previous input")
