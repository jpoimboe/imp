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


FakeResultMessage.__name__ = "ResultMessage"


class FakeClaudeSDKClient:
    """In-memory drop-in for ClaudeSDKClient. Use via async-with."""

    def __init__(self, opts: Any, *, scripted_messages: list[Any]) -> None:
        self.opts = opts
        self._scripted = list(scripted_messages)
        self._pos = 0
        self.queries: list[Any] = []  # whatever was passed to .query()
        self.closed = False

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
        pass

    async def disconnect(self) -> None:
        self.closed = True


def assistant(*blocks: Any) -> FakeAssistantMessage:
    return FakeAssistantMessage(content=list(blocks))


def user(*blocks: Any) -> FakeUserMessage:
    return FakeUserMessage(content=list(blocks))


def result(
    *, is_error: bool = False, subtype: str = "end_turn",
    api_error_status: Any = None, stop_reason: Any = None,
) -> FakeResultMessage:
    return FakeResultMessage(
        subtype=subtype, is_error=is_error,
        api_error_status=api_error_status, stop_reason=stop_reason)


def text(t: str) -> FakeTextBlock:
    return FakeTextBlock(text=t)


def tool_use(id_: str, name: str, args: dict[str, Any]) -> FakeToolUseBlock:
    return FakeToolUseBlock(id=id_, name=name, input=args)


def tool_result(id_: str, content: str, is_error: bool = False) -> FakeToolResultBlock:
    return FakeToolResultBlock(tool_use_id=id_, content=content, is_error=is_error)


def thinking(content: str) -> FakeThinkingBlock:
    return FakeThinkingBlock(thinking=content)
