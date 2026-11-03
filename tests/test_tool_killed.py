from __future__ import annotations

from typing import Any

import pytest

from imp.agents.runner import TextPart, ToolResultBlock
from imp.agents.tool_killed import emit_tool_killed
from imp.engine.state import InMemoryEventLog


def _emitted(name: str, content: Any, *, is_error: bool = False) -> list[dict[str, Any]]:
    elog = InMemoryEventLog()
    emit_tool_killed(elog, "step:a", name, ToolResultBlock("t1", content, is_error=is_error))
    return list(elog.read())


@pytest.mark.parametrize(("name", "content", "signal"), [
    ("bash", '{"returncode": -15, "stdout": ""}', 15),
    ("mcp__imp__bash", '{"returncode": -15}', 15),
    ("bash", [{"type": "text", "text": '{"returncode": -9}'}], 9),
    ("bash", [TextPart('{"returncode": -9}')], 9),
    ("bash", [TextPart('{"returncode":'), TextPart(' -9}')], 9),
])
def test_a_signal_killed_bash_result_emits_tool_killed(
    name: str, content: Any, signal: int,
) -> None:
    [ev] = _emitted(name, content)
    assert ev["type"] == "tool_killed"
    assert ev["agent_id"] == "step:a"
    assert ev["payload"] == {"id": "t1", "name": "bash", "signal": signal}


@pytest.mark.parametrize(("name", "content", "is_error"), [
    ("bash", '{"returncode": -15}', True),
    ("bash_kill", '{"returncode": -15}', False),
    ("bash", '{"returncode": 0}', False),
    ("bash", '{"returncode": null}', False),
    ("bash", '{"stdout": ""}', False),
    ("bash", "Killed", False),
    ("bash", "[-15]", False),
    ("bash", '{"returncode": 1}', False),
    ("bash", [
        {"type": "text", "text": '{"returncode": -9}'},
        {"type": "image", "source": {}},
    ], False),
    ("bash", [{"type": "text", "text": 5}], False),
])
def test_anything_else_emits_nothing(name: str, content: Any, is_error: bool) -> None:
    assert _emitted(name, content, is_error=is_error) == []
