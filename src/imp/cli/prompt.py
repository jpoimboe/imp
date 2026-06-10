"""Prompt abstraction backed by prompt_toolkit.

One Prompt per CLI invocation. Ctrl+C raises KeyboardInterrupt, Ctrl+D raises
EOFError — the CLI's signal handler catches both and triggers save-and-exit.

ESC during the prompt is owned by prompt_toolkit's built-in handling
(meta-prefix in emacs mode; leaves insert mode in vi mode — see editing_mode
plumbing). ESC during the agent-thinking phase (no prompt active) is read by
`EscKeyReader` in `signals.py` and pauses the run. The CLI wraps the
`prompt.ask_async()` call in `coord.tty_quiesced()` so the keystroke reader
releases /dev/tty for the duration of the prompt and re-arms cbreak on exit —
the two never hold the tty in raw/cbreak mode at once.

History is per-session in-memory; persisted history is a post-V0 feature.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any


def _build_session(skills_dir: Any | None = None, *, editing_mode: str = "emacs") -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    history = InMemoryHistory()
    kb = KeyBindings()
    mode = EditingMode.VI if editing_mode == "vi" else EditingMode.EMACS

    hint_shown = False

    @kb.add("c-c")
    def _(event: Any) -> None:  # noqa: ANN401
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

    completer = SlashCompleter(skills_dir) if skills_dir is not None else None
    # No style override: while the user types, the input region keeps the
    # terminal's default fg/bg (a bright-blue caret is the only marker). On
    # submit, erase_when_done wipes prompt_toolkit's whole rendered region —
    # prompt + input + the menu-reservation row that caused the old stray bottom
    # bar — so NOTHING it drew survives in scroll-back. The live driver then
    # reprints the entered turn as a UserMessageNode with the subtle "entered
    # input" background (render_text), giving the after-not-during highlight and
    # one shared rendering for the user turn in live and `imp log`.
    return PromptSession(history=history, key_bindings=kb, completer=completer,  # type: ignore[arg-type]
                         editing_mode=mode, erase_when_done=True)


class SlashCompleter:
    """Completes /help, /skills, and skill names (from skills_dir/*.md) when the
    line starts with '/'. prompt_toolkit Completer protocol (duck-typed to avoid
    importing prompt_toolkit at module import time)."""

    _BUILTINS = ("help", "skills")

    def __init__(self, skills_dir: Any) -> None:
        self._skills_dir = Path(skills_dir)

    def get_completions(self, document: Any, complete_event: Any) -> Iterator[Any]:
        from prompt_toolkit.completion import Completion

        from imp.agents.skills import list_skill_names

        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        prefix = text[1:]
        # dict.fromkeys dedups while preserving order, so a skill named
        # help.md/skills.md doesn't surface twice (builtin + glob).
        names = dict.fromkeys(list(self._BUILTINS) + list_skill_names(self._skills_dir))
        for name in names:
            if name.startswith(prefix):
                yield Completion(name, start_position=-len(prefix))

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
            lambda: _build_session(skills_dir, editing_mode=editing_mode))
        self._session = self._session_factory()

    def _message(self, prompt_text: str | None) -> Any:
        """The prompt caret as prompt_toolkit formatted text: a bright-blue
        ``❯ `` under color, plain otherwise. `None` selects the default caret."""
        from imp.cli import palette as pal

        text = pal.CARET if prompt_text is None else prompt_text
        if self._color:
            from prompt_toolkit.formatted_text import ANSI

            return ANSI(pal.ansi(text, "bright_blue", enabled=True))
        return text

    def ask(self, prompt_text: str | None = None) -> str:
        """Read a single line. Returns the user's text.
        Raises KeyboardInterrupt (Ctrl+C) or EOFError (Ctrl+D) up to caller."""
        result = self._session.prompt(self._message(prompt_text))
        return str(result) if result is not None else ""

    async def ask_async(self, prompt_text: str | None = None) -> str:
        """Async version of ask() — uses prompt_toolkit's native prompt_async()
        so it shares the outer event loop instead of calling asyncio.run()
        internally (which would crash inside anyio.run())."""
        result = await self._session.prompt_async(self._message(prompt_text))
        return str(result) if result is not None else ""

    def history_strings(self) -> list[str]:
        try:
            return list(self._session.history.get_strings())
        except AttributeError:
            return []
