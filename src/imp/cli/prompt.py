"""Prompt abstraction backed by prompt_toolkit.

One Prompt per CLI invocation. ESC returns the ESC_SENTINEL string so
callers can distinguish chat-interrupt (Task 12) from normal text input.
Ctrl+C raises KeyboardInterrupt, Ctrl+D raises EOFError — the CLI's signal
handler (Task 11) catches both and triggers save-and-exit.

History is per-session in-memory; persisted history is a post-V0 feature.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ESC_SENTINEL = "__IMP_ESC__"


def _build_session() -> Any:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    history = InMemoryHistory()
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _esc(event: Any) -> None:
        event.app.exit(result=ESC_SENTINEL)

    return PromptSession(history=history, key_bindings=kb)


class Prompt:
    def __init__(
        self,
        *,
        _session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._session_factory = _session_factory or _build_session
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
