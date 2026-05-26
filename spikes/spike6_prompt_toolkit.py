"""Spike 6: prompt_toolkit-driven input with ESC/Ctrl+C/Ctrl+D bindings.

NOT RUN AUTOMATICALLY — needs an interactive TTY. Run manually:
  python spikes/spike6_prompt_toolkit.py

Goal: verify we can
  1. Read a single line of user input with up-arrow recall.
  2. Bind ESC to return a sentinel.
  3. Distinguish Ctrl+C from Ctrl+D by exit code or sentinel.
  4. Re-enter raw mode multiple times without leaking terminal state.

Run it and try each key. Type 'quit' or hit ESC to exit each loop.

UPDATE THIS COMMENT BLOCK after running:
  ESC: <works / needs escape sequence / requires double-tap>
  CTRL-C: <raises KeyboardInterrupt / other behavior>
  CTRL-D: <raises EOFError / other behavior>
  UP-ARROW RECALL: <works / does not>
"""

from __future__ import annotations

import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings


def main() -> int:
    history = InMemoryHistory()
    history.append_string("hello")
    history.append_string("up-arrow recall test")

    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _esc(event):
        event.app.exit(result="__ESC__")

    session: PromptSession[str] = PromptSession(history=history, key_bindings=kb)

    for i in range(3):
        print(f"\n--- round {i+1} (ESC for sentinel, Ctrl+C/D to quit) ---")
        try:
            ans = session.prompt("you> ")
        except KeyboardInterrupt:
            print("got KeyboardInterrupt (Ctrl+C)")
            return 0
        except EOFError:
            print("got EOFError (Ctrl+D)")
            return 0
        if ans == "__ESC__":
            print("got ESC")
        else:
            print(f"got: {ans!r}")

    print("\nthree clean rounds — prompt_toolkit raw-mode toggling works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
