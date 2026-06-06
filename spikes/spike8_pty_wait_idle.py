"""Spike 8: drive an interactive child via impty and observe readiness.

NOT RUN AUTOMATICALLY. Run manually:
  /home/jpoimboe/imp/.venv/bin/python spikes/spike8_pty_wait_idle.py

Confirms: select-based wait_idle returns while the child blocks at a prompt,
the cursor parks after the prompt text, and a sent answer is echoed/consumed.
"""

from __future__ import annotations

import sys

from impty import TerminalSession

CHILD = (
    "import sys\n"
    "print('banner')\n"
    "name = input('Project name? ')\n"
    "fw = input('Framework? ')\n"
    "print(f'created {name} using {fw}')\n"
)


def main() -> int:
    with TerminalSession([sys.executable, "-c", CHILD]) as s:
        s.wait_idle()
        print("cursor at prompt #1:", s.cursor())
        print("viewport:\n" + s.viewport())
        s.send("todo-api\r")
        s.wait_idle()
        s.send("fastapi\r")
        s.wait_idle()
        print("--- final transcript ---")
        print(s.transcript())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
