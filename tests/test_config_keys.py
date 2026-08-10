from __future__ import annotations

from imp.cli.config_keys import resolve_key, unknown_key_message


def test_home_layer_follows_the_table() -> None:
    theme = resolve_key("theme")
    assert theme is not None
    assert (theme.home, theme.path, theme.read_path) == (
        "global", ("theme",), ("theme",))
    fast = resolve_key("test_cmd_fast")
    assert fast is not None and fast.home == "project"
    workflow = resolve_key("default-workflow")
    assert workflow is not None and workflow.home == "project"
    providers = resolve_key("providers")
    assert providers is not None and providers.home == "global"


def test_provider_envs_reads_back_one_level_above_where_it_is_written() -> None:
    key = resolve_key("provider_envs.anthropic.env")
    assert key is not None
    assert key.path == ("provider_envs", "anthropic", "env")
    # _parse_provider_envs flattens {env: X} to X
    assert key.read_path == ("provider_envs", "anthropic")
    assert key.home == "global"


def test_remote_driver_reads_back_under_its_name_field() -> None:
    key = resolve_key("remote.driver")
    assert key is not None
    assert key.path == ("remote", "driver")
    assert key.read_path == ("remote", "driver", "name")
    assert key.home == "global"


def test_project_only_keys_are_marked() -> None:
    for dotted in ("default-base",):
        key = resolve_key(dotted)
        assert key is not None, dotted
        assert key.project_only is True
        assert key.home == "project"
    theme = resolve_key("theme")
    assert theme is not None and theme.project_only is False


def test_tiers_namespace_accepts_only_real_tiers() -> None:
    key = resolve_key("tiers.high.anthropic")
    assert key is not None
    assert key.path == ("tiers", "high", "anthropic")
    assert key.read_path == ("tiers", "high", "anthropic")
    assert key.home == "global"
    assert resolve_key("tiers.turbo.anthropic") is None


def test_excluded_driver_keys_are_not_settable() -> None:
    assert resolve_key("remote.driver.events") is None
    assert resolve_key("remote.driver.startup_timeout") is None


def test_empty_segments_do_not_resolve() -> None:
    assert resolve_key("vars.") is None
    assert resolve_key("") is None


def test_unknown_key_messages() -> None:
    assert unknown_key_message("thme") == (
        "unknown setting 'thme' (did you mean 'theme'?)")
    assert unknown_key_message("foo") == (
        "unknown setting 'foo' — run 'imp config' to see the settable keys")
    assert unknown_key_message("remote.driver.events") == (
        "unknown setting 'remote.driver.events' (did you mean 'remote.driver'?)")


def _parsed(dotted: str, raw: str) -> object:
    key = resolve_key(dotted)
    assert key is not None, dotted
    return key.parse(raw)


def test_value_parsing_follows_the_value_column() -> None:
    assert _parsed("pythonpath", "src,tests") == ["src", "tests"]
    assert _parsed("providers", "a,b") == ["a", "b"]
    assert _parsed("test_cmd_fast_timeout", "240") == 240
    assert _parsed("max_parallel_test_runs", "2") == 2
    assert _parsed("test_cmd_fast", "a, b") == "a, b"          # literal, not split
    assert _parsed("sandbox.enabled", "TRUE") is True
    assert _parsed("sandbox.network", "false") is False
    assert _parsed("sandbox.extra_readable", "x,y") == ["x", "y"]
    assert _parsed("context.checkpoint_threshold", "0.8") == 0.8
    assert _parsed("context.checkpoint_threshold", "120000") == 120000
    assert _parsed("context.max_reincarnations", "3") == 3
    assert _parsed("vars.x", "true") is True
    assert _parsed("vars.x", "12") == 12
    assert _parsed("vars.x", "12a") == "12a"


def test_facts_keys_are_not_settable() -> None:
    assert resolve_key("facts.stack") is None
    assert unknown_key_message("facts.stack") == (
        "unknown setting 'facts.stack' — run 'imp config' to see the settable keys")


def test_unshapeable_values_pass_through_for_the_reader_to_reject() -> None:
    # No second copy of the validation rules: the writer emits what it was given
    # and imp's own validator produces the message.
    assert _parsed("test_cmd_fast_timeout", "abc") == "abc"
    assert _parsed("sandbox.enabled", "maybe") == "maybe"
    assert _parsed("context.checkpoint_threshold", "soon") == "soon"


def test_worktree_setup_is_a_project_layer_string_key() -> None:
    key = resolve_key("worktree_setup")
    assert key is not None
    assert (key.path, key.read_path) == (("worktree_setup",), ("worktree_setup",))
    assert key.home == "project"
    assert key.project_only is False
    assert key.parse("uv sync --all-extras") == "uv sync --all-extras"
