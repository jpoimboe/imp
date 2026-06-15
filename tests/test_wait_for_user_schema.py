from __future__ import annotations

from imp.agents.tools.wait_for_user import make_wait_for_user_tool


def test_schema_has_no_prompt_property() -> None:
    tool = make_wait_for_user_tool()
    props = tool.input_schema.get("properties", {})
    assert props == {}
    assert tool.input_schema.get("required", []) == []
