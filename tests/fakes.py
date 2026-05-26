"""Fake LLM for mocked end-to-end tests.

langchain-core's built-in fakes don't implement bind_tools, which
create_deep_agent calls. This fake stubs both bind_tools and
with_structured_output as no-ops returning self.

Responses are consumed in FIFO order. If the agent makes more LLM calls
than there are canned responses, raises IndexError (test author's bug).

Helper functions for building Anthropic-shaped AIMessages
--------------------------------------------------------------
Real Anthropic returns content as list[dict] with {type: text, text: ...}
and {type: tool_use, ...} blocks, NOT as a plain string.  The helpers below
produce that shape so tests exercise the deepagent_runner's _extract_text
and _translate_message paths without a real API call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class FakeToolCallingChatModel(BaseChatModel):
    responses: list[AIMessage]
    _i: int = PrivateAttr(default=0)

    model_config: ClassVar[dict[str, Any]] = {
        "extra": "allow", "arbitrary_types_allowed": True,
    }

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def __init__(self, responses: Sequence[AIMessage], **kwargs: Any) -> None:
        super().__init__(responses=list(responses), **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._i >= len(self.responses):
            raise IndexError(
                f"fake LLM ran out of responses (asked {self._i + 1}, "
                f"had {len(self.responses)})"
            )
        msg = self.responses[self._i]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolCallingChatModel:
        return self

    def with_structured_output(
        self, schema: Any, **kwargs: Any,
    ) -> FakeToolCallingChatModel:
        return self


# ---------------------------------------------------------------------------
# Anthropic-shaped AIMessage factories
# ---------------------------------------------------------------------------


def anthropic_text_message(
    text: str,
    tool_calls: list[dict[str, Any]] | None = None,
) -> AIMessage:
    """Build an AIMessage with Anthropic's list-of-blocks content shape.

    ``text`` becomes a ``{type: text}`` block; ``tool_calls`` become
    ``{type: tool_use}`` blocks.  Matches what real Anthropic returns for
    tool-call turns so that tests exercise the _extract_text / _translate_message
    paths in deepagent_runner without a live API call.

    ``tool_calls`` items should have keys: id, name, args (dict).
    """
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    lc_tool_calls: list[Any] = []
    if tool_calls:
        for tc in tool_calls:
            content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc.get("args", {}),
            })
            lc_tool_calls.append({
                "id": tc["id"],
                "name": tc["name"],
                "args": tc.get("args", {}),
                "type": "tool_call",
            })
    return AIMessage(
        content=content if content else [],
        tool_calls=lc_tool_calls,
    )


def anthropic_thinking_message(thinking: str, final_text: str = "") -> AIMessage:
    """Build an AIMessage with a thinking block (no visible text unless
    ``final_text`` provided).  Used to test the no-visible-text rendering path.

    Thinking blocks have ``type: thinking`` in Anthropic's content-block schema.
    The renderer's _extract_text ignores them (only picks up ``type: text``
    blocks), which is the desired behaviour: thinking is not surfaced to users.
    """
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": thinking}]
    if final_text:
        content.append({"type": "text", "text": final_text})
    return AIMessage(content=content)


def anthropic_structured_response_via_text(payload: dict[str, Any]) -> AIMessage:
    """Build an AIMessage whose text channel carries response_format JSON.

    Some models emit response_format JSON via the text channel instead of the
    tool-forced channel.  This exercises the renderer's _is_structured_response
    filter and the runner's _looks_like_structured_response guard, both of which
    should suppress the JSON from appearing as ``[agent] {...}`` in output.
    """
    return AIMessage(content=[{"type": "text", "text": json.dumps(payload)}])
