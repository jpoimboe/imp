"""Vim-style ``.`` (dot) repeat-last-change for imp's vi-mode prompt.

prompt_toolkit 3.0.52 ships no ``.`` command. This module records the
keystrokes of the last buffer change by wrapping the session app's
``KeyProcessor._call_handler`` seam (the same record/replay primitives
prompt_toolkit's own ``@`` macro uses) and binds ``.`` in navigation mode to
replay them. Vi mode only; wired in by ``prompt._build_session``.
"""

from __future__ import annotations

from typing import Any

from prompt_toolkit.filters import vi_navigation_mode
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys

_INSERTISH = (
    InputMode.INSERT,
    InputMode.INSERT_MULTIPLE,
    InputMode.REPLACE,
    InputMode.REPLACE_SINGLE,
)

# Keys whose change must never become the last change: vim's '.' never repeats an
# undo. Redo has no vi binding in 3.0.52; Keys.ControlR is excluded defensively.
_NON_REPEATABLE = {"u", Keys.ControlR}


class DotState:
    """The recorded last change plus scratch fields for boundary tracking.

    One instance per session, shared by the recorder and the '.' binding, so the
    recorded change survives KeyProcessor.reset() across multiple prompt calls.
    """

    def __init__(self) -> None:
        self.last_change: list[KeyPress] | None = None
        self._accum: list[KeyPress] | None = None  # keys of an in-progress insert-change
        self._prefix: list[KeyPress] = []          # pending count/operator keys


def add_dot_binding(kb: KeyBindings, state: DotState) -> None:
    """Add the '.' navigation-mode binding that replays the recorded change."""

    @kb.add(".", filter=vi_navigation_mode, record_in_macro=False)
    def _(event: Any) -> None:
        if not state.last_change:
            return
        keys = [KeyPress(k.key, k.data) for k in state.last_change]
        for k in keys:
            k._dot_replay = True  # type: ignore[attr-defined]  # recorder skips replayed keys
        event.app.key_processor.feed_multiple(keys, first=True)


def install_dot_recorder(session: Any, state: DotState) -> None:
    """Wrap the session app's ``_call_handler`` to record the last change.

    Installed once, after the PromptSession is constructed: the app and its
    key_processor exist at that point and both survive KeyProcessor.reset().
    """
    kp = session.app.key_processor
    app = session.app
    original = kp._call_handler

    original_reset = kp.reset

    def wrapped_reset() -> None:
        # Each prompt starts with one reset(); drop any in-progress insert-change
        # scratch so it can't bleed into the next prompt. last_change persists.
        state._accum = None
        state._prefix = []
        original_reset()

    kp.reset = wrapped_reset

    def wrapped(handler: Any, key_sequence: list[KeyPress]) -> None:
        replay = any(getattr(k, "_dot_replay", False) for k in key_sequence)
        before_mode = app.vi_state.input_mode
        before_text = app.current_buffer.text
        original(handler, key_sequence=key_sequence)
        if replay:
            return
        after_mode = app.vi_state.input_mode
        after_text = app.current_buffer.text
        vi_state = app.vi_state
        pending = kp.arg is not None or vi_state.operator_func is not None
        if state._accum is not None:
            # Mid insert-change: keep appending until mode returns to NAVIGATION.
            state._accum.extend(key_sequence)
            if after_mode == InputMode.NAVIGATION:
                state.last_change = state._accum
                state._accum = None
                state._prefix = []
        elif before_mode == InputMode.NAVIGATION:
            if key_sequence[-1].key in _NON_REPEATABLE:
                # undo/redo is never the last change, and any pending count is
                # discarded (vim's '.' never repeats an undo).
                state._prefix = []
            elif after_mode in _INSERTISH:
                # Entered insert/replace: start an insert-change (count/operator
                # prefix included).
                state._accum = state._prefix + list(key_sequence)
                state._prefix = []
            elif pending:
                # A pending count or operator: stash as a prefix.
                state._prefix.extend(key_sequence)
            elif after_text != before_text:
                # A direct nav-mode change: single-shot change.
                state.last_change = state._prefix + list(key_sequence)
                state._prefix = []
            else:
                # Pure motion / no-op: reset the prefix.
                state._prefix = []

    wrapped._imp_dot_recorder = True  # type: ignore[attr-defined]  # smoke-test seam marker
    kp._call_handler = wrapped
