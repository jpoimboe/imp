"""Fake `Codex` / `Thread` / `ThreadEvent` shapes for unit-testing
CodexAgentRunner (mirrors `tests/cc_fakes.py`).

The runner only introspects `event.type` and a few attributes on each event,
so the fakes don't need to round-trip through openai_codex_sdk's Pydantic
models — they only need to walk like a duck.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass
class FakeReasoningItem:
    type: str = "reasoning"
    text: str = ""


@dataclass
class FakeAgentMessageItem:
    type: str = "agent_message"
    text: str = ""


@dataclass
class FakeMcpToolCallItem:
    type: str = "mcp_tool_call"
    server: str = "imp"
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    status: str = "completed"
    id: str = ""


@dataclass
class FakeCommandExecutionItem:
    type: str = "command_execution"
    command: str = ""
    status: str = "completed"


@dataclass
class FakeFileChangeItem:
    type: str = "file_change"
    path: str = ""
    status: str = "completed"


@dataclass
class FakeWebSearchItem:
    type: str = "web_search"
    query: str = ""


# ── Stream-event envelopes ────────────────────────────────────────────────────


@dataclass
class FakeThreadStartedEvent:
    type: str = "thread.started"
    thread_id: str = "thr-1"


@dataclass
class FakeItemEvent:
    """`event.type == "item.completed"` (or `"item.started"`) carrying an item.
    The runner checks `event.type` and reads `event.item`."""
    type: str
    item: Any


@dataclass
class FakeTurnCompletedEvent:
    type: str = "turn.completed"
    usage: FakeUsage = field(default_factory=FakeUsage)


@dataclass
class FakeTurnFailedEvent:
    type: str = "turn.failed"
    error: dict[str, Any] = field(default_factory=dict)


# ── Thread / Codex client ─────────────────────────────────────────────────────


class FakeStreamedTurn:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    @property
    def events(self) -> _AsyncIterEvents:
        return _AsyncIterEvents(self._events)


class _AsyncIterEvents:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self._pos = 0

    def __aiter__(self) -> _AsyncIterEvents:
        return self

    async def __anext__(self) -> Any:
        if self._pos >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._pos]
        self._pos += 1
        return ev


class FakeThread:
    def __init__(self, scripted_turns: list[list[Any]],
                 on_turn_start: Any = None) -> None:
        self._scripted = list(scripted_turns)
        self._on_turn_start = on_turn_start
        self.aborted = False
        self.run_calls: list[Any] = []

    async def run_streamed(self, prompt: Any, options: Any = None) -> FakeStreamedTurn:
        self.run_calls.append((prompt, options))
        if self._on_turn_start is not None:
            self._on_turn_start()
        if not self._scripted:
            return FakeStreamedTurn([])
        return FakeStreamedTurn(self._scripted.pop(0))


class FakeAbortController:
    def __init__(self) -> None:
        self.aborted = False
        self.signal = self  # the SDK uses `.signal` on the AbortController

    def abort(self) -> None:
        self.aborted = True


class FakeCodex:
    """Client factory injected via `client_factory=` on CodexAgentRunner."""

    def __init__(self, *, scripted_turns: list[list[Any]] | None = None,
                 on_turn_start: Any = None) -> None:
        self._scripted = scripted_turns or []
        self._on_turn_start = on_turn_start
        self.thread: FakeThread | None = None
        self.start_thread_calls: list[Any] = []
        self.options_seen: Any = None

    def start_thread(self, options: Any = None) -> FakeThread:
        self.start_thread_calls.append(options)
        self.thread = FakeThread(self._scripted,
                                 on_turn_start=self._on_turn_start)
        return self.thread


# Helper builders for tests.

def reasoning(text: str) -> FakeItemEvent:
    return FakeItemEvent(type="item.completed",
                         item=FakeReasoningItem(text=text))


def agent_message(text: str) -> FakeItemEvent:
    return FakeItemEvent(type="item.completed",
                         item=FakeAgentMessageItem(text=text))


def mcp_call(name: str, *, args: dict[str, Any] | None = None,
             result: Any = None, item_id: str = "call-1") -> FakeItemEvent:
    return FakeItemEvent(type="item.completed",
                         item=FakeMcpToolCallItem(
                             name=name, arguments=args or {},
                             result=result, id=item_id))


def file_change(path: str) -> FakeItemEvent:
    return FakeItemEvent(type="item.completed",
                         item=FakeFileChangeItem(path=path))


def command_execution(command: str) -> FakeItemEvent:
    return FakeItemEvent(type="item.completed",
                         item=FakeCommandExecutionItem(command=command))


def web_search(query: str = "x") -> FakeItemEvent:
    return FakeItemEvent(type="item.completed",
                         item=FakeWebSearchItem(query=query))


def thread_started(thread_id: str = "thr-1") -> FakeThreadStartedEvent:
    return FakeThreadStartedEvent(thread_id=thread_id)


def turn_completed(
    *, input_tokens: int = 0, output_tokens: int = 0,
    cached_input_tokens: int = 0, reasoning_output_tokens: int = 0,
) -> FakeTurnCompletedEvent:
    return FakeTurnCompletedEvent(usage=FakeUsage(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens))


def turn_failed(error: dict[str, Any]) -> FakeTurnFailedEvent:
    return FakeTurnFailedEvent(error=error)
