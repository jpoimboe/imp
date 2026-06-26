"""Prompt abstraction backed by prompt_toolkit.

One Prompt per CLI invocation. Ctrl+C raises KeyboardInterrupt, Ctrl+D raises
EOFError — the CLI's signal handler catches both and triggers save-and-exit.

A bare ESC is still owned by prompt_toolkit's built-in handling (meta-prefix
in emacs mode; leaves insert mode in vi mode — see editing_mode plumbing).
Alt+Enter (`escape c-m`) is bound by `_build_session` to insert a newline.

History is per-session in-memory; persisted history is a post-V0 feature.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any


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


def _placeholder_text(*, color: bool, text: str = "type your reply") -> str:
    from imp.cli import palette as pal
    return pal.ansi(text, "dim", enabled=color)


def _build_session(skills_dir: Any | None = None, *, editing_mode: str = "emacs",
                   color: bool = False) -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.filters import completion_is_selected
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    history = InMemoryHistory()
    kb = KeyBindings()
    mode = EditingMode.VI if editing_mode == "vi" else EditingMode.EMACS

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
            run_in_terminal(lambda: print("press ctrl-d to exit"))

    @kb.add("c-m", filter=~completion_is_selected)
    def _(event: Any) -> None:
        # Enter submits. Alt+Enter (below) inserts a newline — the pair is the
        # reverse of prompt_toolkit's multiline default (Enter=newline,
        # Meta+Enter=accept). The filter lets Enter still ACCEPT a selected
        # completion (prompt_toolkit's default c-m binding) rather than submit.
        event.app.current_buffer.validate_and_handle()

    @kb.add("c-j", filter=~completion_is_selected)
    def _(event: Any) -> None:
        event.app.current_buffer.validate_and_handle()

    @kb.add("escape", "c-m")
    def _(event: Any) -> None:
        event.app.current_buffer.insert_text("\n")

    completer = SlashCompleter(skills_dir) if skills_dir is not None else None
    # No app bg + reserve_space_for_menu=0: terminal default, no slate bleed; slash menu still pops.
    return PromptSession(history=history, key_bindings=kb, completer=completer,  # type: ignore[arg-type]
                         editing_mode=mode, erase_when_done=True,
                         multiline=True, reserve_space_for_menu=0)


class SlashCompleter:
    """Completes /help, /skills, and user skill names (from skills_dir/user/*.md)
    when the line starts with '/'. prompt_toolkit Completer protocol (duck-typed to
    avoid importing prompt_toolkit at module import time)."""

    _BUILTINS = ("help", "skills")

    def __init__(self, skills_dir: Any) -> None:
        self._skills_dir = Path(skills_dir)

    def get_completions(self, document: Any, complete_event: Any) -> Iterator[Any]:
        from prompt_toolkit.completion import Completion

        from imp.agents.skills import list_user_skills

        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        prefix = text[1:]
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
        _session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._color = color
        self._session_factory = _session_factory or (
            lambda: _build_session(skills_dir, editing_mode=editing_mode, color=color))
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
        coroutine on an out-of-band SIGINT; across the attended-solicit coroutine boundary that KeyboardInterrupt
        escapes imp's except arms and crashes
        anyio.run, leaving the change status="running". Setting it False lets
        imp's InterruptCoordinator SIGINT handler stay installed during the
        prompt and route an out-of-band SIGINT through the coordinator exit path
        (identical to SIGTERM). This also means prompt_toolkit never calls
        loop.remove_signal_handler(SIGINT) on prompt exit, so the handler-deletion
        problem (a88e96e) cannot recur — the rearm_signal_handlers call in
        ask_line_with_slash becomes a harmless defensive no-op (kept)."""
        from prompt_toolkit.formatted_text import ANSI

        from imp.cli import palette as pal

        if question.strip():
            rendered = _prompt_message_text(
                question, color=self._color, width=width,
                display_width=display_width, indent=indent)
            msg: Any = ANSI(rendered) if self._color else rendered
        else:
            msg = self._message(prompt_text, indent=indent)
        cont = " " * (len(indent) + len(pal.CARET))
        ph_text = hint or "type your reply"
        placeholder: Any = (ANSI(_placeholder_text(color=True, text=ph_text)) if self._color
                            else _placeholder_text(color=False, text=ph_text))
        extra: dict[str, Any] = {}
        if bottom_toolbar is not None:
            from prompt_toolkit.styles import Style
            spaced = f"\n{bottom_toolbar}" if bottom_toolbar else bottom_toolbar
            extra["bottom_toolbar"] = ANSI(spaced) if self._color else spaced
            extra["style"] = Style.from_dict({"bottom-toolbar": "noreverse"})
        result = await self._session.prompt_async(
            msg, handle_sigint=False, placeholder=placeholder,
            prompt_continuation=lambda _w, _l, _s: cont, **extra)
        return str(result) if result is not None else ""

    def history_strings(self) -> list[str]:
        try:
            return list(self._session.history.get_strings())
        except AttributeError:
            return []
