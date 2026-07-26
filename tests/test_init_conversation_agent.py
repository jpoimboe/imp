from __future__ import annotations

from imp.assets import bundled_assets_dir, bundled_workflow_dir
from imp.engine.loader import parse_agent_file
from imp.engine.types import AgentDef
from imp.providers.config import _REQUIRED_TIERS


def _agent() -> AgentDef:
    return parse_agent_file(bundled_assets_dir() / "init.md")


def _flat_body() -> str:
    """The prompt body with newlines collapsed, so substring checks survive
    re-wrapping of the source paragraphs."""
    return " ".join(_agent().prompt_template_source.split())


def test_setup_agent_ships_at_the_assets_root_read_only() -> None:
    assert (bundled_assets_dir() / "init.md").is_file()
    assert not (bundled_workflow_dir("default") / "agents"
                / "init-conversation.md").exists()
    a = _agent()
    assert a.name == "init"
    assert set(a.tools) == {"read_file", "glob", "grep", "bash", "symbol_outline"}


def test_setup_agent_output_schema() -> None:
    fields = {f.name: f for f in _agent().output_schema.fields}
    assert set(fields) == {
        "status", "git_init", "default_workflow", "default_base",
        "test_cmd_fast", "pythonpath"}
    assert fields["status"].type == "enum"
    assert set(fields["status"].values) == {"Success", "Error"}
    assert fields["git_init"].type == "bool"
    assert fields["default_workflow"].type == "string"
    assert fields["default_base"].type == "string"
    assert fields["test_cmd_fast"].type == "string"
    assert fields["pythonpath"].type == "object"


def test_setup_agent_body_states_the_facts_it_must_convey() -> None:
    flat = _flat_body()
    assert "Ground every proposal" in flat
    for decision in ("**git**", "**Default workflow**", "**Integration branch**",
                     "**Fast test command**", "**Python import root**"):
        assert decision in flat, decision
    assert "`default`" in flat and "`minimal`" in flat
    assert "--workflow" in flat
    assert "name the signal you used" in flat
    assert "load the `imp-knowledge` skill" in flat
    assert "an empty string means imp runs no tests" in flat   # test-cmd opt-out
    assert "gets an empty list" in flat                        # import-root opt-out


def test_setup_agent_scripts_no_opening_line() -> None:
    flat = _flat_body()
    assert "exactly this line" not in flat
    assert "I'll help you configure imp for this project" not in flat
    assert "in your own words" in flat


def test_setup_agent_declares_only_real_tools() -> None:
    from imp.agents.tools import get_registry
    # Auto-injected (complete_step/sub_agent/load_skill) + provider-native
    # reserved names are valid but absent from the @imp_tool registry.
    reserved = {"complete_step", "sub_agent", "load_skill", "web_search", "web_fetch"}
    allowed = set(get_registry()) | reserved
    assert not set(_agent().tools) - allowed


def test_setup_agent_declares_a_configurable_tier() -> None:
    """run_init resolves the setup model at this tier, so a typo here would
    KeyError mid-setup instead of failing the suite."""
    assert _agent().tier in _REQUIRED_TIERS
