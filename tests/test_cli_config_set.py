from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from imp.cli.config_cmd import layer_label
from imp.cli.config_write import read_back
from imp.cli.main import _build_parser, main
from imp.paths import display_path, project_state_dir
from imp.providers import register_builtin_providers, resolve
from tests._init_config import write_real_init_config


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    write_real_init_config(root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def imp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "imphome"
    monkeypatch.setenv("IMP_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _builtin_providers() -> None:
    # Reset then re-register so stale fakes from other workers can't leak in.
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_flagless_set_follows_the_home_column(
    project: Path, imp_home: Path,
) -> None:
    before = (project / "imp" / "config.yaml").read_text()

    assert main(["config", "set", "theme", "dark"]) == 0

    assert (imp_home / "config.yaml").read_text() == "theme: dark\n"
    assert (project / "imp" / "config.yaml").read_text() == before

    assert main(["config", "set", "test_cmd_fast", "pytest -q"]) == 0

    assert 'test_cmd_fast: "pytest -q"' in (project / "imp" / "config.yaml").read_text()
    assert (imp_home / "config.yaml").read_text() == "theme: dark\n"


def test_set_copy_is_byte_clean_even_on_a_tty(
    project: Path, imp_home: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert main(["config", "set", "test_cmd_fast", "pytest -q"]) == 0
    assert capsys.readouterr().out == (
        'imp/config.yaml: test_cmd_fast: "pytest -q" (was "make test")\n')

    assert main(["config", "set", "test_cmd_fast", "pytest -q"]) == 0
    assert capsys.readouterr().out == (
        'imp/config.yaml: test_cmd_fast: "pytest -q" (unchanged)\n')

    assert main(["config", "set", "--project", "editing_mode", "vi"]) == 0
    assert capsys.readouterr().out == 'imp/config.yaml: editing_mode: "vi" (new)\n'

    assert main(["config", "set", "pythonpath", "src"]) == 0
    assert capsys.readouterr().out == (
        "imp/config.yaml: pythonpath: [src] (was commented out)\n")


def test_unset_copy_and_effect(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "unset", "test_cmd_fast"]) == 0
    assert capsys.readouterr().out == (
        'imp/config.yaml: test_cmd_fast commented out (was "make test")\n')
    assert '# test_cmd_fast: "make test"' in (project / "imp" / "config.yaml").read_text()

    assert main(["config", "unset", "test_cmd_fast"]) == 0
    assert capsys.readouterr().out == (
        "imp/config.yaml: test_cmd_fast is not set (nothing to do)\n")


def test_set_unset_set_leaves_no_stale_lines(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = project / "imp" / "config.yaml"
    before = config.read_text()

    assert main(["config", "unset", "test_cmd_fast"]) == 0
    assert main(["config", "set", "test_cmd_fast", "make test"]) == 0
    capsys.readouterr()

    assert config.read_text() == before        # set and unset are exact inverses

    assert main(["config", "unset", "test_cmd_fast"]) == 0
    assert main(["config", "set", "test_cmd_fast", "pytest -q"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        'imp/config.yaml: test_cmd_fast: "pytest -q" (was commented out)')

    text = config.read_text()
    assert text.count("test_cmd_fast: ") == 1
    assert len(text.splitlines()) == len(before.splitlines())


def test_project_only_keys_reject_the_global_layer(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "set", "--global", "default-base", "dev"]) == 2
    assert capsys.readouterr().err == (
        "imp: 'default-base' is a project setting; drop --global\n")

    assert not (imp_home / "config.yaml").exists()
    assert main(["config", "set", "default-base", "dev"]) == 0
    assert read_back(project / "imp" / "config.yaml", ("default-base",)) == "dev"


def test_unknown_settings_are_rejected_and_write_nothing(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    before = (project / "imp" / "config.yaml").read_text()

    assert main(["config", "set", "thme", "dark"]) == 2
    assert capsys.readouterr().err == (
        "imp: unknown setting 'thme' (did you mean 'theme'?)\n")

    assert main(["config", "set", "foo", "1"]) == 2
    assert capsys.readouterr().err == (
        "imp: unknown setting 'foo' — run 'imp config' to see the settable keys\n")

    assert main(["config", "set", "remote.driver.events", "all"]) == 2
    assert capsys.readouterr().err == (
        "imp: unknown setting 'remote.driver.events' "
        "(did you mean 'remote.driver'?)\n")

    assert main(["config", "set", "remote.driver.startup_timeout", "5"]) == 2
    assert capsys.readouterr().err == (
        "imp: unknown setting 'remote.driver.startup_timeout' — "
        "run 'imp config' to see the settable keys\n")

    assert main(["config", "set", "facts.stack", "python"]) == 2
    assert capsys.readouterr().err == (
        "imp: unknown setting 'facts.stack' — run 'imp config' to see the settable keys\n")

    assert (project / "imp" / "config.yaml").read_text() == before
    assert not (imp_home / "config.yaml").exists()


def test_missing_project_config_and_the_transition_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, imp_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "bare"
    (root / "imp").mkdir(parents=True)
    monkeypatch.chdir(root)

    assert main(["config", "set", "test_cmd_fast", "x"]) == 2
    assert capsys.readouterr().err == (
        f"imp: no imp/config.yaml in {root.resolve()} — run 'imp init' first\n")

    assert main(["config", "unset", "test_cmd_fast"]) == 2
    assert capsys.readouterr().err == (
        f"imp: no imp/config.yaml in {root.resolve()} — run 'imp init' first\n")
    assert not (root / "imp" / "config.yaml").exists()

    from imp.engine.worktree import checked_out_branch
    assert checked_out_branch(root) is None  # so the hint omits the branch clause
    (root / "imp" / "project.yaml").write_text("default-base: main\n")

    assert main(["config", "unset", "test_cmd_fast"]) == 2
    assert capsys.readouterr().err == (
        f"imp: imp/project.yaml found in {root.resolve()} but imp now reads "
        f"imp/config.yaml — rename it with "
        f"`git -C {root.resolve()} mv imp/project.yaml imp/config.yaml`\n")
    assert not (root / "imp" / "config.yaml").exists()


def test_global_set_creates_the_file_while_unset_creates_nothing(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "unset", "theme"]) == 0
    assert capsys.readouterr().out == (
        f"{layer_label(imp_home / 'config.yaml')}: theme is not set "
        "(nothing to do)\n")
    assert not (imp_home / "config.yaml").exists()

    assert main(["config", "set", "theme", "light"]) == 0
    assert (imp_home / "config.yaml").read_text() == "theme: light\n"


def test_yaml_1_2_values_survive_the_writer(project: Path, imp_home: Path) -> None:
    assert main(["config", "set", "vars.window", "1:30"]) == 0

    config = project / "imp" / "config.yaml"
    assert read_back(config, ("vars", "window")) == "1:30"


def test_a_value_that_never_reads_back_aborts_and_leaves_the_file_untouched(
    project: Path, imp_home: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = project / "imp" / "config.yaml"
    before = config.read_text()
    monkeypatch.setattr("imp.cli.config_write.read_back",
                        lambda *args, **kwargs: "something else")

    assert main(["config", "set", "vars.x", "1"]) == 2
    assert capsys.readouterr().err == (
        "imp: refusing to write imp/config.yaml — "
        "'vars.x' would not read back as given\n")
    assert config.read_text() == before


def test_invalid_value_reports_the_validator_message_alone(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    before = (project / "imp" / "config.yaml").read_text()

    assert main(["config", "set", "theme", "purple"]) == 2
    err = capsys.readouterr().err
    assert err == "imp: 'theme' must be 'dark', 'light', or 'ansi'\n"
    assert "config-verify" not in err
    assert not (imp_home / "config.yaml").exists()

    assert main(["config", "set", "test_cmd_fast_timeout", "abc"]) == 2
    assert capsys.readouterr().err == (
        "imp: 'test_cmd_fast_timeout' must be a positive integer "
        "(seconds, e.g. 120)\n")
    assert (project / "imp" / "config.yaml").read_text() == before


def test_unset_notes_a_shadowing_layer(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = project_state_dir(project)
    (state / "config.yaml").write_text("test_cmd_fast: make local\n")

    assert main(["config", "unset", "test_cmd_fast"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0] == 'imp/config.yaml: test_cmd_fast commented out (was "make test")'
    assert out[1] == (
        f"note: test_cmd_fast is also set in "
        f"{display_path(state / 'config.yaml')}, which takes "
        "precedence (effective: make local)")


def test_shadow_note_names_the_layer_that_wins(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = project_state_dir(project)
    (state / "config.yaml").write_text("theme: ansi\n")

    assert main(["config", "set", "--project", "theme", "dark"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "imp/config.yaml: theme: \"dark\" (new)"
    assert out[1] == (
        f"note: theme is also set in {display_path(state / 'config.yaml')}, "
        "which takes precedence (effective: ansi)")


def test_table_routing_and_value_shapes(project: Path, imp_home: Path) -> None:
    assert main(["config", "set", "provider_envs.anthropic.env", "MY_KEY"]) == 0
    assert main(["config", "set", "remote.driver", "telegram"]) == 0
    assert main(["config", "set", "max_parallel_test_runs", "2"]) == 0
    assert main(["config", "set", "pythonpath", "src,tests"]) == 0
    assert main(["config", "set", "--global", "sandbox.enabled", "TRUE"]) == 0

    global_file = imp_home / "config.yaml"
    project_file = project / "imp" / "config.yaml"
    assert read_back(global_file, ("provider_envs", "anthropic")) == "MY_KEY"
    assert read_back(global_file, ("remote", "driver", "name")) == "telegram"
    assert read_back(global_file, ("sandbox", "enabled")) is True
    assert read_back(project_file, ("max_parallel_test_runs",)) == 2
    assert "pythonpath: [src, tests]\n" in project_file.read_text()


def test_a_namespace_takes_more_than_one_entry_and_unsets_back_out(
    project: Path, imp_home: Path,
) -> None:
    for dotted, value in (("tiers.high.anthropic", "opus"),
                          ("tiers.low.anthropic", "haiku"),
                          ("provider_envs.a.env", "A_KEY"),
                          ("provider_envs.b.env", "B_KEY")):
        assert main(["config", "set", dotted, value]) == 0, dotted

    global_file = imp_home / "config.yaml"
    assert read_back(global_file, ("tiers", "high", "anthropic")) == "opus"
    assert read_back(global_file, ("tiers", "low", "anthropic")) == "haiku"
    assert read_back(global_file, ("provider_envs", "a")) == "A_KEY"
    assert read_back(global_file, ("provider_envs", "b")) == "B_KEY"

    assert main(["config", "unset", "tiers.low.anthropic"]) == 0
    assert read_back(global_file, ("tiers", "low")) is None
    assert read_back(global_file, ("tiers", "high", "anthropic")) == "opus"

    assert main(["config", "unset", "tiers.high.anthropic"]) == 0
    assert read_back(global_file, ("tiers",)) is None
    assert "# tiers:" in global_file.read_text()


def test_unset_refuses_a_key_that_shares_a_flow_line(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    imp_home.mkdir(parents=True)
    global_file = imp_home / "config.yaml"
    global_file.write_text("tiers:\n  high: {anthropic: opus, openai: gpt}\n")
    before = global_file.read_text()

    assert main(["config", "unset", "tiers.high.openai"]) == 2

    assert capsys.readouterr().err == (
        f"imp: refusing to write {layer_label(global_file)} — "
        "'tiers.high.openai' shares its line with other settings; "
        "edit the file by hand\n")
    assert global_file.read_text() == before


def test_unset_refuses_inside_a_multi_line_flow_block(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    imp_home.mkdir(parents=True)
    global_file = imp_home / "config.yaml"
    global_file.write_text("tiers:\n  high: {\n    anthropic: opus\n  }\n")
    before = global_file.read_text()

    assert main(["config", "unset", "tiers.high.anthropic"]) == 2

    assert capsys.readouterr().err == (
        f"imp: refusing to write {layer_label(global_file)} — "
        "'tiers.high.anthropic' is inside a multi-line { } block; "
        "edit the file by hand\n")
    assert global_file.read_text() == before


def test_a_malformed_target_file_reports_invalid_yaml(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = project / "imp" / "config.yaml"
    config.write_text("theme: dark\ntheme: light\n")

    assert main(["config", "set", "--project", "editing_mode", "vi"]) == 2

    err = capsys.readouterr().err
    assert err.startswith("imp: invalid YAML in config.yaml: ")
    assert "Traceback" not in err
    assert config.read_text() == "theme: dark\ntheme: light\n"


def test_local_set_creates_the_file_and_wins_the_stack(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = project_state_dir(project)          # also creates imp_home
    label = display_path(state / "config.yaml")
    assert not (state / "config.yaml").exists()
    # the project layer already sets test_cmd_fast to "make test"; seed the
    # global layer with the same key so the render proves local beats both
    (imp_home / "config.yaml").write_text("test_cmd_fast: make global\n")

    assert main(["config", "set", "--local", "test_cmd_fast", "make local"]) == 0
    assert capsys.readouterr().out == (
        f'{label}: test_cmd_fast: "make local" (new)\n')
    assert read_back(state / "config.yaml", ("test_cmd_fast",)) == "make local"
    assert 'test_cmd_fast: "make test"' in (
        project / "imp" / "config.yaml").read_text()
    assert (imp_home / "config.yaml").read_text() == "test_cmd_fast: make global\n"

    assert main(["config"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.strip().startswith("test_cmd_fast "))
    assert "make local" in line
    assert line.rstrip().endswith(f"[{label}]")


def test_local_set_creates_a_missing_state_dir_under_the_override(
    project: Path, imp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "elsewhere" / "state"
    monkeypatch.setenv("IMP_STATE_DIR", str(state))

    assert main(["config", "set", "--local", "theme", "dark"]) == 0

    assert read_back(state / "config.yaml", ("theme",)) == "dark"
    assert sorted(p.name for p in state.iterdir()) == ["config.yaml"]


def test_local_unset_comments_the_key_out(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = project_state_dir(project)
    (state / "config.yaml").write_text("theme: ansi\n")
    label = display_path(state / "config.yaml")

    assert main(["config", "unset", "--local", "theme"]) == 0

    assert capsys.readouterr().out == (
        f'{label}: theme commented out (was "ansi")\n')
    assert "# theme: ansi" in (state / "config.yaml").read_text()
    assert read_back(state / "config.yaml", ("theme",)) is None


def test_local_unset_does_nothing_when_config_is_absent(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = project_state_dir(project)
    label = display_path(state / "config.yaml")

    assert main(["config", "unset", "--local", "theme"]) == 0

    assert capsys.readouterr().out == f"{label}: theme is not set (nothing to do)\n"
    assert not (state / "config.yaml").exists()


def test_project_only_keys_reject_the_local_layer(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state = project_state_dir(project)

    assert main(["config", "set", "--local", "default-base", "dev"]) == 2

    assert capsys.readouterr().err == (
        "imp: 'default-base' is a project setting; drop --local\n")
    assert not (state / "config.yaml").exists()


def test_the_three_scopes_are_mutually_exclusive(
    project: Path, imp_home: Path,
) -> None:
    for pair in (("--project", "--local"), ("--global", "--local"),
                 ("--project", "--global")):
        with pytest.raises(SystemExit) as excinfo:
            main(["config", "set", *pair, "theme", "dark"])
        assert excinfo.value.code == 2


def test_bare_config_and_explain_are_unchanged(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "settings:" in out
    assert out.splitlines()[0] == (
        f"project state: {display_path(project_state_dir(project))}")

    assert main(["config", "--explain", "high"]) == 0
    explained = capsys.readouterr().out
    assert explained.splitlines()[0] == "tier high:"
    assert "project state:" not in explained

    assert main(["config", "--all"]) == 0
    assert "tiers:" in capsys.readouterr().out


def test_rejected_global_set_writes_no_config_and_accepted_set_leaves_no_scratch(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "set", "--global", "theme", "purple"]) == 2
    assert capsys.readouterr().err == "imp: 'theme' must be 'dark', 'light', or 'ansi'\n"
    assert not (imp_home / "config.yaml").exists()

    assert main(["config", "set", "--global", "theme", "light"]) == 0
    capsys.readouterr()
    assert imp_home.is_dir()
    assert (imp_home / "config.yaml").read_text() == "theme: light\n"
    assert sorted(p.name for p in imp_home.iterdir()) == ["config.yaml", "projects"]


def _config_action_parser(name: str) -> argparse.ArgumentParser:
    parser = _build_parser()
    config = next(action.choices["config"] for action in parser._actions
                  if isinstance(action, argparse._SubParsersAction))
    return next(action.choices[name] for action in config._actions
                if isinstance(action, argparse._SubParsersAction))


def test_a_local_write_has_nothing_above_it(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Local is the top layer; a --local write must not name itself — or any
    lower layer — as a shadow (spec AC 39)."""
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("theme: light\n")
    state = project_state_dir(project)
    (state / "config.yaml").write_text("theme: ansi\n")
    label = display_path(state / "config.yaml")

    assert main(["config", "set", "--local", "theme", "dark"]) == 0
    assert capsys.readouterr().out == f'{label}: theme: "dark" (was "ansi")\n'

    assert main(["config", "unset", "--local", "theme"]) == 0
    assert capsys.readouterr().out == (
        f'{label}: theme commented out (was "dark")\n')


def test_set_worktree_setup_lands_in_the_project_layer(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "set", "worktree_setup", "uv sync --all-extras"]) == 0
    assert capsys.readouterr().out == (
        'imp/config.yaml: worktree_setup: "uv sync --all-extras" (new)\n')
    assert read_back(project / "imp" / "config.yaml", ("worktree_setup",)) == (
        "uv sync --all-extras")


@pytest.mark.parametrize("key", ["test_cmd_fast", "test_cmd_slow", "worktree_setup"])
def test_set_a_command_key_false_lands_as_a_boolean(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
    key: str,
) -> None:
    assert main(["config", "set", key, "false"]) == 0
    capsys.readouterr()
    cfg = project / "imp" / "config.yaml"
    assert read_back(cfg, (key,)) is False
    assert main(["config", "unset", key]) == 0
    capsys.readouterr()
    assert read_back(cfg, (key,)) is None


def test_set_pythonpath_false_lands_as_a_boolean_not_a_one_item_list(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "set", "pythonpath", "false"]) == 0
    capsys.readouterr()
    assert read_back(project / "imp" / "config.yaml", ("pythonpath",)) is False


def test_set_worktree_setup_empty_is_refused_and_leaves_the_file_alone(
    project: Path, imp_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = project / "imp" / "config.yaml"
    before = cfg.read_text()
    assert main(["config", "set", "worktree_setup", ""]) == 2
    assert capsys.readouterr().err == (
        "imp: 'worktree_setup' must be a non-empty string "
        '(e.g. "uv sync --all-extras") '
        "or false to declare this project needs none\n")
    assert cfg.read_text() == before


def test_help_copy_matches_the_spec() -> None:
    parser = _build_parser()
    config = next(action.choices["config"] for action in parser._actions
                  if isinstance(action, argparse._SubParsersAction))
    group = next(action for action in config._actions
                 if isinstance(action, argparse._SubParsersAction))
    helps = {choice.dest: choice.help for choice in group._choices_actions}
    assert helps["set"] == (
        "Set a configuration value in the project or global config file.")
    assert helps["unset"] == (
        "Comment out a configuration value in the project or global config file.")
    assert config.description is not None
    assert "provenance" in config.description        # test_cli_help_polish
    assert "`imp config set`" in config.description  # no longer read-only

    flags = {action.option_strings[0]: action.help
             for action in _config_action_parser("set")._actions
             if action.option_strings}
    assert flags["--project"] == "Write to imp/config.yaml (this repo)."
    assert flags["--global"] == "Write to ~/.imp/config.yaml (this machine)."
    assert flags["--local"] == (
        "Write to ~/.imp/projects/<name>/config.yaml (this checkout).")
