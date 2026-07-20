from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import parse_agent_file


def test_init_pythonpath_agent_parses_and_declares_schema() -> None:
    path = bundled_workflow_dir("imp") / "agents" / "init-pythonpath.md"
    agent = parse_agent_file(path)
    assert agent.name == "init-pythonpath"
    assert agent.tier == "medium"
    assert set(agent.tools) == {"read_file", "glob", "grep", "bash", "symbol_outline"}
    fields = {f.name: f for f in agent.output_schema.fields}
    assert fields["status"].type == "enum"
    assert fields["status"].values == ("Success", "Error")
    assert fields["pythonpath"].type == "object"
    assert fields["pythonpath"].required is True
