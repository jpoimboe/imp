"""argparse help in imp's palette: chrome dim, content plain, wordmark gold."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from imp.cli import palette as pal

TAGLINE = "imp — multi-agent software development workflow"

_SECTION_HEADINGS = frozenset({"positional arguments:", "options:"})


def _paint(text: str, *, wordmark: bool) -> str:
    painted: list[str] = []
    for i, line in enumerate(text.split("\n")):
        if wordmark and line == TAGLINE:
            line = pal.ansi("imp", "wordmark") + line[len("imp"):]
        elif i == 0 and line.startswith("usage:"):
            line = pal.ansi("usage:", "dim") + line[len("usage:"):]
        elif line in _SECTION_HEADINGS:
            line = pal.ansi(line, "dim")
        painted.append(line)
    return "\n".join(painted)


class ImpHelpFormatter(argparse.HelpFormatter):
    """Auto-wrapping variant, for flowing-prose descriptions."""

    _wordmark = False

    def add_argument(self, action: argparse.Action) -> None:
        # Skip _decolor: absent before 3.14; imp sets color=False so invocations carry no escapes.
        if action.help is not argparse.SUPPRESS:
            invocation_lengths = [
                len(self._format_action_invocation(action)) + self._current_indent
            ]
            for subaction in self._iter_indented_subactions(action):
                invocation_lengths.append(
                    len(self._format_action_invocation(subaction)) + self._current_indent
                )
            self._action_max_length = max(self._action_max_length, max(invocation_lengths))
            self._add_item(self._format_action, [action])

    def format_help(self) -> str:
        text = super().format_help()
        if pal.should_use_color(sys.stdout):
            text = _paint(text, wordmark=self._wordmark)
        # Probed: add_subparsers() formats at construction, when stdout may lack isatty.
        if getattr(sys.stdout, "isatty", lambda: False)():
            text += "\n"
        return text


class ImpRawHelpFormatter(ImpHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Verbatim description and epilog, where line breaks carry meaning."""


class ImpTopLevelHelpFormatter(ImpRawHelpFormatter):
    _wordmark = True


class ImpParser(argparse.ArgumentParser):
    """`add_subparsers` passes `type(self)` down, styling the whole tree."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", ImpHelpFormatter)
        super().__init__(*args, **kwargs)
        self.color = False

    def format_usage(self) -> str:
        # A usage line printed by error() is not help output; no trailing blank.
        return super().format_usage().rstrip("\n") + "\n"
