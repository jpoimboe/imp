"""Fake ClaudeSDKClient for unit-testing ClaudeCodeAgentRunner.

The real SDK exposes:
  - ClaudeSDKClient(opts).__aenter__ / __aexit__
  - .query(prompt: str | AsyncIterable[dict])
  - .receive_response() → AsyncIterator[Message]

The fake mirrors that surface with a canned script:
  - query() records the prompt for assertions
  - receive_response() yields canned SDK-shaped messages

The canned messages are constructed via small dataclasses that mimic the
SDK's AssistantMessage / UserMessage / ResultMessage shape. The runner only
introspects `type(msg).__name__` and a few public attributes, so as long as
those line up the runner is happy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FakeTextBlock:
    text: str
    # Runner uses type(b).__name__ == "TextBlock"


FakeTextBlock.__name__ = "TextBlock"  # name match — real SDK uses no decorator


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


FakeToolUseBlock.__name__ = "ToolUseBlock"


@dataclass
class FakeToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


FakeToolResultBlock.__name__ = "ToolResultBlock"


@dataclass
class FakeThinkingBlock:
    thinking: str


FakeThinkingBlock.__name__ = "ThinkingBlock"


@dataclass
class FakeAssistantMessage:
    content: list[Any]
    message_id: str | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None
    stop_reason: Any = None


FakeAssistantMessage.__name__ = "AssistantMessage"


@dataclass
class FakeUserMessage:
    content: list[Any]


FakeUserMessage.__name__ = "UserMessage"


@dataclass
class FakeResultMessage:
    """End-of-turn marker. The runner reacts to this by checking
    state.complete_step_called and breaking the loop. is_error / api_error_status
    / stop_reason mirror the SDK fields the P2 termination path reads."""
    subtype: str = "end_turn"
    duration_ms: int = 0
    usage: dict[str, Any] | None = None
    is_error: bool = False
    api_error_status: Any = None
    stop_reason: Any = None
    total_cost_usd: float = 0.0
    duration_api_ms: int = 0
    num_turns: int = 0
    model_usage: dict[str, Any] | None = None
    permission_denials: Any = None
    errors: Any = None


FakeResultMessage.__name__ = "ResultMessage"


class FakeClaudeSDKClient:
    """In-memory drop-in for ClaudeSDKClient. Use via async-with."""

    def __init__(self, opts: Any, *, scripted_messages: list[Any],
                 context_usage: Any = None,
                 context_usage_raises: bool = False) -> None:
        self.opts = opts
        self._scripted = list(scripted_messages)
        self._pos = 0
        self.queries: list[Any] = []  # whatever was passed to .query()
        self.closed = False
        self.interrupts = 0  # count of interrupt() calls
        self._context_usage = context_usage
        self._context_usage_raises = context_usage_raises

    async def __aenter__(self) -> FakeClaudeSDKClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        self.closed = True
        return False

    async def query(self, prompt: Any) -> None:
        """Capture the prompt; if it's an async iterable, drain it into a list
        so tests can assert on what was sent."""
        if hasattr(prompt, "__aiter__"):
            drained: list[Any] = []
            async for item in prompt:
                drained.append(item)
            self.queries.append(drained)
        else:
            self.queries.append(prompt)

    async def receive_response(self) -> Any:
        # Mirror the real SDK: a single receive_response() call yields one turn's
        # messages up to and INCLUDING the next ResultMessage, then terminates.
        # The cursor persists, so the provider's next per-turn call resumes the
        # script at the following message (issue 7).
        while self._pos < len(self._scripted):
            msg = self._scripted[self._pos]
            self._pos += 1
            yield msg
            if type(msg).__name__ == "ResultMessage":
                return

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def get_context_usage(self) -> Any:
        if self._context_usage_raises:
            raise RuntimeError("simulated get_context_usage failure")
        return self._context_usage

    async def disconnect(self) -> None:
        self.closed = True


def assistant(*blocks: Any, message_id: str | None = None,
              model: str | None = None, usage: dict[str, Any] | None = None,
              stop_reason: Any = None) -> FakeAssistantMessage:
    return FakeAssistantMessage(
        content=list(blocks), message_id=message_id, model=model,
        usage=usage, stop_reason=stop_reason)


def user(*blocks: Any) -> FakeUserMessage:
    return FakeUserMessage(content=list(blocks))


def result(
    *, is_error: bool = False, subtype: str = "end_turn",
    api_error_status: Any = None, stop_reason: Any = None,
    total_cost_usd: float = 0.0, duration_ms: int = 0,
    duration_api_ms: int = 0, num_turns: int = 0,
    usage: dict[str, Any] | None = None,
    model_usage: dict[str, Any] | None = None,
    permission_denials: Any = None, errors: Any = None,
) -> FakeResultMessage:
    return FakeResultMessage(
        subtype=subtype, is_error=is_error,
        api_error_status=api_error_status, stop_reason=stop_reason,
        total_cost_usd=total_cost_usd, duration_ms=duration_ms,
        duration_api_ms=duration_api_ms, num_turns=num_turns,
        usage=usage, model_usage=model_usage,
        permission_denials=permission_denials, errors=errors)


def text(t: str) -> FakeTextBlock:
    return FakeTextBlock(text=t)


def tool_use(id_: str, name: str, args: dict[str, Any]) -> FakeToolUseBlock:
    return FakeToolUseBlock(id=id_, name=name, input=args)


def tool_result(id_: str, content: str, is_error: bool = False) -> FakeToolResultBlock:
    return FakeToolResultBlock(tool_use_id=id_, content=content, is_error=is_error)


def thinking(content: str) -> FakeThinkingBlock:
    return FakeThinkingBlock(thinking=content)


@dataclass
class FakeContextUsage:
    rawMaxTokens: int
    percentage: float
    totalTokens: int


def context_usage(*, raw_max_tokens: int, percentage: float,
                  total_tokens: int) -> FakeContextUsage:
    return FakeContextUsage(
        rawMaxTokens=raw_max_tokens, percentage=percentage,
        totalTokens=total_tokens)


class CooperativeClient:
    """Models the cooperative interrupt: first receive_response() parks until
    interrupt() fires, then yields ResultMessage(subtype="error_during_execution").
    After the steer reply is queried, the next receive_response() yields
    after_steer. Tracks interrupt()/disconnect() in order for ordering assertions
    and queries to prove the SAME live client continued (no re-run)."""

    def __init__(self, opts: Any, *, after_steer: list[Any],
                 order: list[str] | None = None) -> None:
        import asyncio
        self.opts = opts
        self.queries: list[Any] = []
        self.closed = False
        self.disconnected = False
        self.interrupt_calls = 0
        self.order = order if order is not None else []
        self._after_steer = list(after_steer)
        self._turn = 0
        self._released = asyncio.Event()

    async def __aenter__(self) -> CooperativeClient:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        self.closed = True
        return False

    async def query(self, prompt: Any) -> None:
        if hasattr(prompt, "__aiter__"):
            drained: list[Any] = []
            async for item in prompt:
                drained.append(item)
            self.queries.append(drained)
        else:
            self.queries.append(prompt)

    async def receive_response(self) -> Any:
        self._turn += 1
        if self._turn == 1:
            await self._released.wait()
            yield FakeResultMessage(
                subtype="error_during_execution", is_error=True)
            return
        for msg in self._after_steer:
            yield msg
            if type(msg).__name__ == "ResultMessage":
                return

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self.order.append("interrupt")
        self._released.set()

    async def disconnect(self) -> None:
        self.disconnected = True
        self.order.append("disconnect")

    async def get_context_usage(self) -> Any:
        return None
