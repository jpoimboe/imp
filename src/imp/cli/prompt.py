"""Prompt abstraction backed by prompt_toolkit.

One Prompt per CLI invocation. Ctrl+C raises KeyboardInterrupt, Ctrl+D raises
EOFError — the CLI's signal handler catches both and triggers save-and-exit.
ESC is left to prompt_toolkit's built-in handling (meta-prefix in emacs mode;
leaves insert mode in vi mode — see editing_mode plumbing).

History is per-session in-memory; persisted history is a post-V0 feature.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any


def _build_session(skills_dir: Any | None = None, *, editing_mode: str = "emacs",
                   color: bool = False) -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    history = InMemoryHistory()
    kb = KeyBindings()
    mode = EditingMode.VI if editing_mode == "vi" else EditingMode.EMACS

    completer = SlashCompleter(skills_dir) if skills_dir is not None else None
    # bright-blue background on the input region (the default style class),
    # paralleling NODE_COLORS["user"] in palette.py so the live prompt and the
    # `imp log` replay of UserMessageNode look the same. Omitted under NO_COLOR
    # / non-TTY (`color=False`) so the default unstyled input is shown.
    style = Style.from_dict({"": "bg:ansibrightblue"}) if color else None
    return PromptSession(history=history, key_bindings=kb, completer=completer,  # type: ignore[arg-type]
                         editing_mode=mode, style=style)


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
        self._session_factory = _session_factory or (
            lambda: _build_session(skills_dir, editing_mode=editing_mode,
                                   color=color))
        self._session = self._session_factory()

    def ask(self, prompt_text: str = "> ") -> str:
        """Read a single line. Returns the user's text.
        Raises KeyboardInterrupt (Ctrl+C) or EOFError (Ctrl+D) up to caller."""
        result = self._session.prompt(prompt_text)
        return str(result) if result is not None else ""

    async def ask_async(self, prompt_text: str = "> ") -> str:
        """Async version of ask() — uses prompt_toolkit's native prompt_async()
        so it shares the outer event loop instead of calling asyncio.run()
        internally (which would crash inside anyio.run())."""
        result = await self._session.prompt_async(prompt_text)
        return str(result) if result is not None else ""

    def history_strings(self) -> list[str]:
        try:
            return list(self._session.history.get_strings())
        except AttributeError:
            return []
