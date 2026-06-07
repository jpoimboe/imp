"""impty — drive any interactive CLI/TUI on a real pty and read its
faithfully-rendered screen. Provisional name (see plan); the product name is
an open item. Deterministic plumbing only: spawn, send raw keystrokes, read
lines()/cell()/cursor() snapshots across scroll and alt-screen regimes. No
semantic translation.
"""

from __future__ import annotations

from impty.screen import Regime
from impty.session import TerminalSession
from impty.view import Cell

__all__ = ["Cell", "Regime", "TerminalSession"]
