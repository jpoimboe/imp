"""Fake LLM for mocked end-to-end tests.

langchain-core's built-in fakes don't implement bind_tools, which
create_deep_agent calls. This fake stubs both bind_tools and
with_structured_output as no-ops returning self.

Responses are consumed in FIFO order. If the agent makes more LLM calls
than there are canned responses, raises IndexError (test author's bug).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class FakeToolCallingChatModel(BaseChatModel):
    responses: list[AIMessage]
    _i: int = PrivateAttr(default=0)

    model_config = {"extra": "allow", "arbitrary_types_allowed": True}

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

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeToolCallingChatModel":
        return self

    def with_structured_output(
        self, schema: Any, **kwargs: Any,
    ) -> "FakeToolCallingChatModel":
        return self
