"""Spike: can the interactive input line carry a subtle background WITHOUT the
old bug (a stray colored bar at the bottom, separate from the cursor line)?

Run one approach in a pty and drive it with impty to capture a PNG:

    python spikes/spike_input_bg.py <approach>

approaches:
  old        Style({"": "bg:..."})           — reproduce the old full-app bg bug
  menu0      same bg + reserve_space_for_menu=0 + bottom_toolbar=None
  inputonly  bg only on the buffer text via a named input-field style class
  erase      no live bg; erase_when_done=True, then WE reprint the entered line
             with a subtle bg (the "after entry, not during" idea)

The script prints a banner, prompts once, then prints a fake agent reply so the
captured scrollback shows what the user's turn looks like AFTER submission.
"""
from __future__ import annotations

import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style

# A subtle slate background (not the old shouting bright-blue). 24-bit so it's
# identical everywhere; a hair lighter than a #1e1e1e terminal so the line reads
# as "entered input" without glare.
_SUBTLE_BG = "bg:#2b2f3a"
_CARET = ANSI("\x1b[94m> \x1b[0m")


def _session(approach: str) -> PromptSession:
    if approach == "old":
        return PromptSession(style=Style.from_dict({"": "bg:ansibrightblue"}))
    if approach == "menu0":
        return PromptSession(
            style=Style.from_dict({"": _SUBTLE_BG}),
            reserve_space_for_menu=0,
            bottom_toolbar=None,
        )
    if approach == "inputonly":
        # Style only the input-field text class, not the global "" default, so the
        # bg hugs the typed text rather than filling the whole app region.
        return PromptSession(
            style=Style.from_dict({"input-field": _SUBTLE_BG}),
            include_default_pygments_style=False,
        )
    if approach == "erase":
        # prompt_toolkit erases its whole rendered region (prompt + input + menu
        # reservation) on completion, so nothing it drew survives in scroll-back —
        # WE reprint the entered line afterwards. erase_when_done is a constructor
        # arg, not a prompt() kwarg.
        return PromptSession(reserve_space_for_menu=0, erase_when_done=True)
    return PromptSession(reserve_space_for_menu=0)


def main() -> int:
    approach = sys.argv[1] if len(sys.argv) > 1 else "menu0"
    print(f"[spike approach={approach}] prior agent output above the input.\n")
    sess = _session(approach)

    if approach == "erase":
        line = sess.prompt(_CARET)
        # Reprint the user's turn ourselves with a subtle full-line bg that ends
        # at newline — no app-region fill, so there's no stray bottom bar.
        print(f"\x1b[48;2;43;47;58m\x1b[94m> \x1b[0m\x1b[48;2;43;47;58m{line}\x1b[0m")
    else:
        line = sess.prompt(_CARET)

    print("\n• Got it — here's the agent's reply after your turn.")
    print(f"  (you typed: {line!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
