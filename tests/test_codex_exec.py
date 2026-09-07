"""Unit tests for imp's own `codex exec --json` driver: argv, env, spawn, read loop,
stderr drain, and group-reaping teardown."""
from __future__ import annotations

from imp.providers.codex_exec import ThreadOptions, build_command_args

_OPTS_HIGH = ThreadOptions(
    model="gpt-5-codex", sandbox_mode="read-only", approval_policy="never",
    network_access_enabled=False, model_reasoning_effort="high")
_OPTS_LOW = ThreadOptions(
    model="gpt-5-codex", sandbox_mode="read-only", approval_policy="never",
    network_access_enabled=False)


def test_medium_tier_argv_is_exactly_the_production_flag_list() -> None:
    assert build_command_args(_OPTS_HIGH, None) == [
        "exec", "--json", "--model", "gpt-5-codex", "--sandbox", "read-only",
        "--config", 'model_reasoning_effort="high"',
        "--config", "sandbox_workspace_write.network_access=false",
        "--config", 'approval_policy="never"']


def test_an_unset_reasoning_effort_drops_only_that_config_pair() -> None:
    args = build_command_args(_OPTS_LOW, None)
    assert not any("model_reasoning_effort" in a for a in args)
    assert args == [
        "exec", "--json", "--model", "gpt-5-codex", "--sandbox", "read-only",
        "--config", "sandbox_workspace_write.network_access=false",
        "--config", 'approval_policy="never"']


def test_network_access_renders_as_a_lowercase_json_bool() -> None:
    enabled = ThreadOptions(
        model="m", sandbox_mode="workspace-write", approval_policy="never",
        network_access_enabled=True)
    assert "sandbox_workspace_write.network_access=true" in build_command_args(enabled, None)
    assert "sandbox_workspace_write.network_access=false" in build_command_args(_OPTS_LOW, None)


def test_resuming_appends_the_thread_id_as_the_final_two_arguments() -> None:
    args = build_command_args(_OPTS_HIGH, "thr-42")
    assert args[-2:] == ["resume", "thr-42"]
    assert args[:-2] == build_command_args(_OPTS_HIGH, None)


def test_argv_carries_the_current_json_flag_and_no_web_search_option() -> None:
    args = build_command_args(_OPTS_HIGH, "thr-42")
    assert "--json" in args
    assert "--experimental-json" not in args
    assert not any("features.web_search_request" in a for a in args)
