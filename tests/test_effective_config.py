"""Floor + three-layer merge with per-leaf provenance (spec §2)."""
from pathlib import Path

import pytest

from imp.paths import project_state_dir
from imp.providers import register_builtin_providers, resolve
from imp.providers.config import parse_config_layer
from imp.providers.effective import (
    load_effective_config,
    resolve_default_workflow,
)
from imp.providers.paths import global_config_path

_REPO_CONFIG = Path(__file__).resolve().parents[1] / "imp" / "config.yaml"


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_floor_only_populates_all_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))  # no global config
    eff = load_effective_config()
    cfg = eff.config
    assert cfg.tiers["high"]["anthropic"] == "claude-opus-5[1m]"
    assert cfg.tiers["high"]["openai"] == "gpt-5.5"
    # Floor provenance points at provider code.
    assert "provider code" in str(eff.provenance[("tiers", "high", "anthropic")].file)


def test_project_leaf_override_is_additive_over_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("tiers:\n  high: {anthropic: opus-pinned}\n")
    eff = load_effective_config(control_root=proj)
    # Overridden leaf comes from project; sibling vendor stays at the floor.
    assert eff.config.tiers["high"]["anthropic"] == "opus-pinned"
    assert eff.config.tiers["high"]["openai"] == "gpt-5.5"
    assert eff.provenance[("tiers", "high", "anthropic")].file == proj / "imp" / "config.yaml"
    assert "provider code" in str(eff.provenance[("tiers", "high", "openai")].file)


def test_local_beats_global_beats_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    imp_home = tmp_path / "imphome"
    imp_home.mkdir(parents=True)
    (imp_home / "config.yaml").write_text("theme: light\nproviders: [openai]\n")
    monkeypatch.setenv("IMP_HOME", str(imp_home))
    control = tmp_path / "control"
    (control / "imp").mkdir(parents=True)
    (control / "imp" / "config.yaml").write_text("theme: dark\n")
    state = project_state_dir(control)
    (state / "config.yaml").write_text("theme: ansi\n")
    eff = load_effective_config(control_root=control)
    assert eff.config.theme == "ansi"           # local wins
    assert eff.config.providers == ("openai",)  # from global
    assert eff.provenance[("theme",)].file == state / "config.yaml"


def test_global_config_path_respects_imp_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMP_HOME", "/tmp/x")
    assert global_config_path() == Path("/tmp/x/config.yaml")


def test_global_config_path_defaults_under_the_home_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMP_HOME", raising=False)
    assert global_config_path() == Path.home() / ".imp" / "config.yaml"


def test_test_cmd_fields_flow_from_project_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: config.yaml test_cmd_* land on Config, which the engine bundles
    # into TestGates / IMP_TEST_CMD_* for the gate subprocesses.
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text(
        "test_cmd_fast: make check\n"
        "test_cmd_slow: make check && make test-live\n"
        "test_cmd_fast_timeout: 120\n"
        "test_cmd_slow_timeout: 300\n")
    eff = load_effective_config(control_root=proj)
    assert eff.config.test_cmd_fast == "make check"
    assert eff.config.test_cmd_slow == "make check && make test-live"
    assert eff.config.test_cmd_fast_timeout == 120
    assert eff.config.test_cmd_slow_timeout == 300


def test_test_cmd_fields_default_to_none_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    assert eff.config.test_cmd_fast is None
    assert eff.config.test_cmd_slow is None
    assert eff.config.test_cmd_fast_timeout is None
    assert eff.config.test_cmd_slow_timeout is None


def test_pythonpath_flows_from_project_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "config.yaml").write_text("pythonpath: [src, vendor]\n")
    eff = load_effective_config(control_root=proj)
    assert eff.config.pythonpath == ("src", "vendor")


def test_pythonpath_defaults_to_empty_tuple_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    eff = load_effective_config()
    assert eff.config.pythonpath == ()


def test_repo_config_uses_tiered_test_cmds() -> None:
    # imp's own shipped config layer, read directly: the effective-config stack
    # is not what this asserts.
    layer, _src = parse_config_layer(_REPO_CONFIG)
    assert layer["test_cmd_fast"] == "make check"
    assert layer["test_cmd_slow"] == "make check && make test-live"
    assert layer["test_cmd_fast_timeout"] == 240
    assert layer["test_cmd_slow_timeout"] == 600
    # Guard against a regression to the removed verify_command/verify_timeout surface.
    raw = _REPO_CONFIG.read_text()
    assert "verify_command" not in raw and "verify_timeout" not in raw


def test_repo_config_declares_pythonpath_src() -> None:
    # imp is the first consumer of the general knob: src-layout, one `src` root
    # covers both src/imp and src/impty. Must be declared since the env-level
    # auto-shadow branch was deleted.
    layer, _src = parse_config_layer(_REPO_CONFIG)
    assert layer["pythonpath"] == ["src"]


def test_memory_files_do_not_reach_the_merged_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory is a worktree payload loaded at its own call sites against a root
    the caller names — never a Config field, which would let a caller pair it
    with the wrong root."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    proj = tmp_path / "proj"
    (proj / "imp" / "memory").mkdir(parents=True)
    (proj / "imp" / "memory" / "all.md").write_text("a note")
    (proj / "imp" / "config.yaml").write_text("theme: dark\n")
    eff = load_effective_config(control_root=proj)
    assert eff.config.theme == "dark"           # settings still merge
    assert not hasattr(eff.config, "memory")    # the payload does not ride along


def test_no_parameter_can_aim_a_settings_load_at_a_worktree(tmp_path: Path) -> None:
    """The project_root parameter is removed, not repointed (spec §1)."""
    with pytest.raises(TypeError, match="project_root"):
        load_effective_config(project_root=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="project_root"):
        resolve_default_workflow(  # type: ignore[call-arg]
            project_root=tmp_path, control_root=None)


def test_local_layer_reads_from_the_state_dir_not_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    control = tmp_path / "control"
    (control / "imp").mkdir(parents=True)
    (control / "imp" / "config.yaml").write_text("theme: dark\n")
    (control / ".imp").mkdir()
    (control / ".imp" / "config.yaml").write_text("theme: ansi\n")
    state = project_state_dir(control)
    (state / "config.yaml").write_text("theme: light\n")
    eff = load_effective_config(control_root=control)
    assert eff.config.theme == "light"
    assert eff.provenance[("theme",)].file == state / "config.yaml"


def test_a_command_outside_a_project_claims_no_slug_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.main import main
    from imp.engine import worktree

    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    # Assumes tmp_path is not inside a git work tree: under a --basetemp within
    # the checkout, main()'s stack dumper resolves a control root and claims a
    # slug before dispatch, which would look like a regression here. git walks
    # UP, so this must probe the resolver, not just scratch/.git.
    with pytest.raises(worktree.PreconditionError):
        worktree.find_control_root(scratch)
    main(["list"])
    assert not (tmp_path / "imphome" / "projects").exists()
