# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""argparse help in imp's palette: chrome dim, content plain, wordmark gold."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

from imp.cli import palette as pal

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

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
    _target_stream: SupportsWrite[str] | None = None

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
        stream = self._target_stream if self._target_stream is not None else sys.stdout
        text = super().format_help()
        if pal.should_use_color(stream):
            text = _paint(text, wordmark=self._wordmark)
        # Probed: add_subparsers() formats at construction, when stdout may lack isatty.
        if getattr(stream, "isatty", lambda: False)():
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
        # Pre-assign before super().__init__: on 3.14, add_argument('-h') triggers _get_formatter.
        self._help_target: SupportsWrite[str] | None = None
        super().__init__(*args, **kwargs)
        self.color = False

    def _get_formatter(self) -> argparse.HelpFormatter:
        formatter = super()._get_formatter()
        if isinstance(formatter, ImpHelpFormatter):
            formatter._target_stream = self._help_target
        return formatter

    def print_help(self, file: SupportsWrite[str] | None = None) -> None:
        self._help_target = file if file is not None else sys.stdout
        try:
            super().print_help(file)
        finally:
            self._help_target = None

    def format_usage(self) -> str:
        # A usage line printed by error() is not help output; no trailing blank.
        return super().format_usage().rstrip("\n") + "\n"
