"""Smoke tests for the test fake LLM."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from tests.fakes import FakeToolCallingChatModel


def test_returns_canned_message() -> None:
    canned = AIMessage(content="hello")
    fake = FakeToolCallingChatModel(responses=[canned])
    result = fake.invoke([HumanMessage("hi")])
    assert result.content == "hello"


def test_bind_tools_returns_self() -> None:
    fake = FakeToolCallingChatModel(responses=[AIMessage(content="ok")])
    bound = fake.bind_tools([])
    assert bound is fake


def test_with_structured_output_returns_self() -> None:
    fake = FakeToolCallingChatModel(responses=[AIMessage(content="ok")])
    bound = fake.with_structured_output(dict)
    assert bound is fake


def test_consumes_responses_in_order() -> None:
    canned = [AIMessage(content="first"), AIMessage(content="second")]
    fake = FakeToolCallingChatModel(responses=canned)
    assert fake.invoke([HumanMessage("a")]).content == "first"
    assert fake.invoke([HumanMessage("b")]).content == "second"
