"""argparse help in imp's palette: chrome dim, content plain, wordmark gold."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from imp.cli import palette as pal

TAGLINE = "imp — multi-agent software development workflow"

_HEADINGS = ("usage:", "positional arguments:", "options:")


def _paint(text: str, *, wordmark: bool) -> str:
    painted: list[str] = []
    for line in text.split("\n"):
        if wordmark and line == TAGLINE:
            line = pal.ansi("imp", "wordmark") + line[len("imp"):]
        else:
            for heading in _HEADINGS:
                if line.startswith(heading):
                    line = pal.ansi(heading, "dim") + line[len(heading):]
                    break
        painted.append(line)
    return "\n".join(painted)


class ImpHelpFormatter(argparse.HelpFormatter):
    """Auto-wrapping variant, for flowing-prose descriptions."""

    _wordmark = False

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
