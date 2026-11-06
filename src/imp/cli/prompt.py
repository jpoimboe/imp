# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Prompt abstraction backed by prompt_toolkit.

One Prompt per CLI invocation. Ctrl+C raises KeyboardInterrupt, Ctrl+D raises
EOFError — the CLI's signal handler catches both and triggers save-and-exit.

In vi mode a bare ESC enters normal mode, shown by a block cursor (bar in
insert, underline in replace); in emacs mode prompt_toolkit discards a lone
ESC after its key timeout.
Alt+Enter (`escape c-m`) is bound by `_build_session` to insert a newline.

History is in-memory and seeded at construction from the quest's durable
event record (the seed opening plus each human-typed line), so up-arrow
recalls prior lines across an imp resume. In vi mode, ``j``/``k``/``gj``/``gk``
are display-row motions bounded to the buffer; history is reached only via
arrows or Ctrl-P/Ctrl-N.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

EMPTY_PROMPT_HINT = "press ctrl-d to pause & exit"
DEFAULT_HINT = "type a message, or /help"
OPENING_HINTS = {
    "new": "describe what you want built, or /help",
    "chat": "ask anything, or /help",
}


def _prompt_message_text(question: str, *, color: bool, width: int = 80,
                         display_width: int = 80, indent: str = "") -> str:
    from imp.cli import palette as pal
    from imp.cli.render_text import render_question_block
    q = question.strip()
    if not q:
        return indent + pal.caret(enabled=color)
    lines = render_question_block(q, width, 0, color, display_width)
    body = "\n".join(indent + ln for ln in lines)
    return body + "\n\n" + indent + pal.caret(enabled=color)


def _placeholder_text(*, color: bool, text: str = DEFAULT_HINT) -> str:
    from imp.cli import palette as pal
    return pal.ansi(text, "dim", enabled=color)


def _build_session(skills_dir: Any | None = None, *, editing_mode: str = "emacs",
                   color: bool = False,
                   roster_models: Callable[[], list[str]] | None = None,
                   empty_hint: str = EMPTY_PROMPT_HINT) -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.cursor_shapes import ModalCursorShapeConfig
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    history = InMemoryHistory()
    kb = KeyBindings()
    mode = EditingMode.VI if editing_mode == "vi" else EditingMode.EMACS

    dot_state: Any = None
    if mode == EditingMode.VI:
        from imp.cli import vi_dot, vi_motion

        dot_state = vi_dot.DotState()
        vi_dot.add_dot_binding(kb, dot_state)
        vi_motion.add_motion_bindings(kb)

    hint_shown = False

    @kb.add("c-c")
    def _(event: Any) -> None:
        # Typed Ctrl-C must NEVER raise (the default binding does). Non-empty
        # buffer → clear it; empty buffer → hint at Ctrl-D, once per session.
        nonlocal hint_shown
        buf = event.app.current_buffer
        if buf.text:
            buf.reset()                    # clear the typed line in place
            return
        if not hint_shown:
            hint_shown = True
            from prompt_toolkit.application import run_in_terminal
            # Fire-and-forget: run_in_terminal schedules via ensure_future; the
            # returned Future is intentionally not awaited from this sync handler.
            run_in_terminal(lambda: print(empty_hint))

    @kb.add("c-m")
    def _(event: Any) -> None:
        # Enter always submits (even with a completion selected); Alt+Enter inserts a newline.
        event.app.current_buffer.validate_and_handle()

    @kb.add("c-j")
    def _(event: Any) -> None:
        event.app.current_buffer.validate_and_handle()

    @kb.add("escape", "c-m")
    def _(event: Any) -> None:
        event.app.current_buffer.insert_text("\n")

    completer = (
        SlashCompleter(skills_dir, roster_models) if skills_dir is not None else None)
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.output import ColorDepth

    from imp.cli import palette as pal

    class _FloatingToolbarSession(PromptSession[str]):
        """Ends the layout with an unconditional filler after the toolbar, so the
        renderer's forced full-height fill lands there and the toolbar hugs the input."""

        def _create_layout(self) -> Any:
            layout = super()._create_layout()
            layout.container.children.append(  # type: ignore[attr-defined]
                # take_using_weights costs ~weight x visual rows per render, so
                # the weight must stay modest while still dominating the input
                # window's weight of 1 (a huge value made a multi-line paste
                # render take ~20s). At 100 the input gets zero extra rows on
                # any terminal up to ~100 rows tall (appearance identical);
                # above that a single-line input may gain one cosmetic blank row.
                Window(height=Dimension(weight=100)))
            return layout

    # prompt_toolkit defaults to 8-bit and ignores COLORTERM; force 24-bit on truecolor themes.
    color_depth = ColorDepth.DEPTH_24_BIT if pal.active_theme() in ("dark", "light") else None
    # No app bg + reserve_space_for_menu=0: terminal default, no slate bleed; slash menu still pops.
    session: Any = _FloatingToolbarSession(
        history=history, key_bindings=kb, completer=completer,  # type: ignore[arg-type]
        editing_mode=mode, erase_when_done=True,
        multiline=True, reserve_space_for_menu=0, color_depth=color_depth,
        cursor=ModalCursorShapeConfig() if mode == EditingMode.VI else None)
    if dot_state is not None:
        from imp.cli import vi_dot

        vi_dot.install_dot_recorder(session, dot_state)
    return session


class SlashCompleter:
    """Completes client-side slash commands and user skill names when the line starts with '/'."""

    _BUILTINS = (
        "help", "skills", "pause", "archive", "clear", "model", "sandbox",
        "provider",
        "compact", "abandon")

    def __init__(
        self, skills_dir: Any,
        roster_models: Callable[[], list[str]] | None = None,
    ) -> None:
        self._skills_dir = Path(skills_dir)
        self._roster_models = roster_models
        self._model_cache: list[str] | None = None

    def _model_candidates(self) -> list[str]:
        """Roster model names for /model completion, read at most once per session."""
        if self._model_cache is None:
            self._model_cache = (
                list(self._roster_models()) if self._roster_models is not None else [])
        return self._model_cache

    def get_completions(self, document: Any, complete_event: Any) -> Iterator[Any]:
        from prompt_toolkit.completion import Completion

        from imp.agents.skills import list_user_skills

        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        rest = text[1:]
        # A space means the command name is typed: complete the /model ARGUMENT token.
        if " " in rest:
            cmd, _, arg = rest.partition(" ")
            if cmd == "model":
                for name in self._model_candidates():
                    if name.startswith(arg):
                        yield Completion(
                            name, start_position=-len(arg), display_meta="")
            return
        prefix = rest
        # Builtins first (no description); user skills after. setdefault keeps the
        # builtin entry if a user skill shares its name (e.g. a skills.md), so it
        # never surfaces twice.
        candidates: dict[str, str | None] = {}
        for builtin in self._BUILTINS:
            candidates.setdefault(builtin, None)
        for stem, description in list_user_skills(self._skills_dir):
            candidates.setdefault(stem, description)
        for name, description in candidates.items():
            if name.startswith(prefix):
                yield Completion(
                    name, start_position=-len(prefix),
                    display_meta=description or "",
                )

    async def get_completions_async(
        self, document: Any, complete_event: Any
    ) -> AsyncIterator[Any]:
        """prompt_toolkit's async REPL (PromptSession.prompt_async) drives
        completions through this, not get_completions. The real Completer base
        class supplies it for free, but we duck-type to avoid an import-time
        prompt_toolkit dependency — so we mirror the base class by delegating."""
        for item in self.get_completions(document, complete_event):
            yield item


class Prompt:
    def __init__(
        self,
        *,
        skills_dir: Any | None = None,
        editing_mode: str = "emacs",
        color: bool = False,
        quest: Any | None = None,
        empty_hint: str = EMPTY_PROMPT_HINT,
        _session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._color = color
        roster_models: Callable[[], list[str]] | None = None
        if quest is not None:
            def _load_roster() -> list[str]:
                from imp.cli.model_command import roster_model_names
                return roster_model_names(quest)
            roster_models = _load_roster
        self._session_factory = _session_factory or (
            lambda: _build_session(
                skills_dir, editing_mode=editing_mode, color=color,
                roster_models=roster_models, empty_hint=empty_hint))
        self._session = self._session_factory()

    def _message(self, prompt_text: str | None, indent: str = "") -> Any:
        """The prompt caret as prompt_toolkit formatted text. The default caret
        (`prompt_text is None`) is the gold `> ` (pal.CARET); an explicit non-empty
        `prompt_text` is styled the same gold. Empty text adds no escapes."""
        from imp.cli import palette as pal

        text = pal.CARET if prompt_text is None else prompt_text
        if self._color:
            from prompt_toolkit.formatted_text import ANSI

            styled = pal.ansi(text, "wordmark", enabled=True) if text else ""
            return ANSI(indent + styled)
        return indent + text

    def ask(self, prompt_text: str | None = None) -> str:
        """Read input. Returns the user's text.
        Raises KeyboardInterrupt (Ctrl+C) or EOFError (Ctrl+D) up to caller."""
        # handle_sigint=False: see ask_async for the rationale. The sync path is
        # not used interactively, but we keep the two consistent.
        result = self._session.prompt(self._message(prompt_text), handle_sigint=False)
        return str(result) if result is not None else ""

    async def ask_async(self, prompt_text: str | None = None, *,
                        question: str = "", width: int = 80, display_width: int = 80,
                        indent: str = "", hint: str | None = None,
                        bottom_toolbar: Any = None) -> str:
        """Async version of ask() — uses prompt_toolkit's native prompt_async()
        so it shares the outer event loop instead of calling asyncio.run()
        internally (which would crash inside anyio.run()).

        handle_sigint=False: typed Ctrl-C is handled by the `c-c` KEY binding (a
        key event in _build_session), independent of handle_sigint —
        handle_sigint only governs OS-signal handling. With the default
        handle_sigint=True, prompt_toolkit installs its OWN SIGINT handler for
        the prompt's duration and raises KeyboardInterrupt inside the prompt app
        coroutine on an out-of-band SIGINT; across the attended-solicit coroutine
        boundary that KeyboardInterrupt escapes imp's except arms and crashes
        anyio.run, leaving the quest status="running". Setting it False lets
        imp's InterruptCoordinator SIGINT handler stay installed during the
        prompt and route an out-of-band SIGINT through the coordinator exit path
        (identical to SIGTERM). This also means prompt_toolkit never calls
        loop.remove_signal_handler(SIGINT) on prompt exit, so the handler-deletion
        problem (a88e96e) cannot recur — the rearm_signal_handlers call in
        solicit_line becomes a harmless defensive no-op (kept)."""
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text

        from imp.cli import palette as pal

        if question.strip():
            rendered = _prompt_message_text(
                question, color=self._color, width=width,
                display_width=display_width, indent=indent)
            msg: Any = ANSI(rendered) if self._color else rendered
        else:
            msg = self._message(prompt_text, indent=indent)
        cont = " " * (len(indent) + len(pal.CARET))
        ph_text = hint or DEFAULT_HINT
        placeholder: Any = (ANSI(_placeholder_text(color=True, text=ph_text)) if self._color
                            else _placeholder_text(color=False, text=ph_text))
        extra: dict[str, Any] = {}
        if bottom_toolbar is not None:
            from prompt_toolkit.styles import Style
            spaced = f"\n{bottom_toolbar}" if bottom_toolbar else bottom_toolbar
            # Inline fragment-level noreverse beats the default reverse-video
            # bottom-toolbar class regardless of class-merge precedence.
            if self._color:
                extra["bottom_toolbar"] = to_formatted_text(ANSI(spaced), style="noreverse")
            else:
                extra["bottom_toolbar"] = spaced
            extra["style"] = Style.from_dict({"bottom-toolbar": "noreverse"})
        stdin = sys.stdin
        if stdin.isatty():
            # Drop typed-ahead kernel-buffered bytes so they don't auto-submit into this prompt.
            try:
                import termios
            except ImportError:
                pass
            else:
                termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
        result = await self._session.prompt_async(
            msg, handle_sigint=False, placeholder=placeholder,
            prompt_continuation=lambda _w, _l, _s: cont, **extra)
        return str(result) if result is not None else ""

    def history_strings(self) -> list[str]:
        try:
            return list(self._session.history.get_strings())
        except AttributeError:
            return []

    def seed_history(self, lines: list[str]) -> None:
        for line in lines:
            self._session.history.append_string(line)
