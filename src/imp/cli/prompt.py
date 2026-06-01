"""Prompt abstraction backed by prompt_toolkit.

One Prompt per CLI invocation. ESC returns the ESC_SENTINEL string so
callers can distinguish chat-interrupt (Task 12) from normal text input.
Ctrl+C raises KeyboardInterrupt, Ctrl+D raises EOFError — the CLI's signal
handler (Task 11) catches both and triggers save-and-exit.

History is per-session in-memory; persisted history is a post-V0 feature.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ESC_SENTINEL = "__IMP_ESC__"


def _build_session(skills_dir: Any | None = None) -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    history = InMemoryHistory()
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _esc(event: Any) -> None:
        event.app.exit(result=ESC_SENTINEL)

    completer = SlashCompleter(skills_dir) if skills_dir is not None else None
    return PromptSession(history=history, key_bindings=kb, completer=completer)  # type: ignore[arg-type]


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


class Prompt:
    def __init__(
        self,
        *,
        skills_dir: Any | None = None,
        _session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._session_factory = _session_factory or (lambda: _build_session(skills_dir))
        self._session = self._session_factory()

    def ask(self, prompt_text: str = "> ") -> str:
        """Read a single line. Returns user text, or ESC_SENTINEL.
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
