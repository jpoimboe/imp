from __future__ import annotations

import logging
from pathlib import Path

import pytest

from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from imp.providers import web_native as wn


@pytest.fixture(autouse=True)
def _clear_warned_unsupported():
    wn._warned_unsupported.clear()


def _agent(tools: frozenset[str]) -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(fields=(), source=loc),
        prompt_template_source="", source_path=Path("a.md"), source=loc)


def test_reserved_names() -> None:
    assert frozenset({"web_search", "web_fetch"}) == wn.WEB_TOOL_NAMES


def test_declared_web_tools_filters_and_orders() -> None:
    got = wn.declared_web_tools(_agent(frozenset({"bash", "web_search", "web_fetch"})))
    assert sorted(got) == ["web_fetch", "web_search"]


def test_capability_ok_for_anthropic_both() -> None:
    # Should not raise.
    wn.check_web_capability("anthropic", _agent(frozenset({"web_search", "web_fetch"})))


def test_capability_openai_web_fetch_degrades(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="imp.providers.web_native"):
        wn.check_web_capability("openai", _agent(frozenset({"web_fetch"})))  # no raise
    assert any("web_fetch" in r.getMessage() and "openai" in r.getMessage() for r in caplog.records)


def test_capability_codex_web_fetch_degrades(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="imp.providers.web_native"):
        wn.check_web_capability("codex", _agent(frozenset({"web_fetch"})))  # no raise
    assert any("codex" in r.getMessage() and "web_fetch" in r.getMessage() for r in caplog.records)


def test_capability_degrades_warns_once(caplog) -> None:
    agent = _agent(frozenset({"web_search", "web_fetch"}))
    with caplog.at_level(logging.WARNING, logger="imp.providers.web_native"):
        wn.check_web_capability("openai", agent)
        wn.check_web_capability("openai", agent)
    hits = [r for r in caplog.records if "web_fetch" in r.getMessage()]
    assert len(hits) == 1
    assert "openai" in hits[0].getMessage()


def test_openai_specs_drops_web_fetch_keeps_web_search() -> None:
    # openai_web_tool_specs only emits web_search; web_fetch is not an openai-native tool.
    assert wn.openai_web_tool_specs(_agent(frozenset({"web_search", "web_fetch"}))) == [
        {"type": "web_search"}]


def test_capability_openai_web_search_ok() -> None:
    wn.check_web_capability("openai", _agent(frozenset({"web_search"})))


def test_capability_codex_web_search_ok() -> None:
    # codex supports model-hosted web_search (option B) — must not raise.
    wn.check_web_capability("codex", _agent(frozenset({"web_search"})))


def test_anthropic_specs() -> None:
    specs = wn.anthropic_web_tool_specs(_agent(frozenset({"web_search", "web_fetch"})))
    types = {s["type"] for s in specs}
    assert "web_search_20250305" in types
    assert any(t.startswith("web_fetch") for t in types)
    assert all(s["name"] in {"web_search", "web_fetch"} for s in specs)


def test_openai_specs_web_search_only() -> None:
    assert wn.openai_web_tool_specs(_agent(frozenset({"web_search"}))) == [
        {"type": "web_search"}]
    assert wn.openai_web_tool_specs(_agent(frozenset({"bash"}))) == []


def test_cc_enabled_builtins() -> None:
    got = wn.cc_enabled_web_builtins(_agent(frozenset({"web_search"})))
    assert got == ["WebSearch"]
    got2 = wn.cc_enabled_web_builtins(_agent(frozenset({"web_search", "web_fetch"})))
    assert sorted(got2) == ["WebFetch", "WebSearch"]
