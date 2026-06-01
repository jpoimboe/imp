from __future__ import annotations

from imp.agents.runner import ToolResultBlock
from imp.engine.state import InMemoryEventLog


def _results(elog: InMemoryEventLog) -> list[dict]:
    return [e for e in elog.read() if e["type"] == "tool_result"]


def test_tool_result_without_citations_has_no_citations_key() -> None:
    elog = InMemoryEventLog()
    elog.append_tool_result(agent_id="a", block=ToolResultBlock("t1", "ok"))
    payload = _results(elog)[0]["payload"]
    assert payload == {"id": "t1", "content": "ok", "error": False}
    assert "citations" not in payload


def test_tool_result_with_citations_merges_array() -> None:
    elog = InMemoryEventLog()
    cites = [{"url": "https://x.test", "title": "X", "cited_text": "hi"}]
    elog.append_tool_result(
        agent_id="a", block=ToolResultBlock("t2", "body"), citations=cites)
    payload = _results(elog)[0]["payload"]
    assert payload["id"] == "t2"
    assert payload["content"] == "body"
    assert payload["error"] is False
    assert payload["citations"] == cites


def test_tool_result_empty_citations_list_is_omitted() -> None:
    elog = InMemoryEventLog()
    elog.append_tool_result(
        agent_id="a", block=ToolResultBlock("t3", "body"), citations=[])
    assert "citations" not in _results(elog)[0]["payload"]
