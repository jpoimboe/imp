"""Floor + three-layer merge with per-leaf provenance (spec §2)."""
from pathlib import Path

import pytest

from imp.providers import register_builtin_providers, resolve
from imp.providers.effective import load_effective_config, user_config_path


@pytest.fixture(autouse=True)
def _registry() -> None:
    resolve.reset_providers_for_tests()
    register_builtin_providers()


def test_floor_only_populates_all_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # no user config
    eff = load_effective_config(project_root=None, control_root=None)
    cfg = eff.config
    assert cfg.tiers["high"]["anthropic"] == "opus"
    assert cfg.tiers["high"]["openai"] == "gpt-5.5"
    # Floor provenance points at provider code.
    assert "provider code" in str(eff.provenance[("tiers", "high", "anthropic")].file)


def test_project_leaf_override_is_additive_over_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "project.yaml").write_text("tiers:\n  high: {anthropic: opus-pinned}\n")
    eff = load_effective_config(project_root=proj, control_root=None)
    # Overridden leaf comes from project; sibling vendor stays at the floor.
    assert eff.config.tiers["high"]["anthropic"] == "opus-pinned"
    assert eff.config.tiers["high"]["openai"] == "gpt-5.5"
    assert eff.provenance[("tiers", "high", "anthropic")].file.name == "project.yaml"
    assert "provider code" in str(eff.provenance[("tiers", "high", "openai")].file)


def test_local_beats_user_beats_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "imp").mkdir(parents=True)
    (xdg / "imp" / "config.yaml").write_text("theme: light\nproviders: [openai]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "project.yaml").write_text("theme: dark\n")
    control = tmp_path / "control"
    (control / ".imp").mkdir(parents=True)
    (control / ".imp" / "config.yaml").write_text("theme: ansi\n")
    eff = load_effective_config(project_root=proj, control_root=control)
    assert eff.config.theme == "ansi"  # local wins
    assert eff.config.providers == ("openai",)  # from user
    assert eff.provenance[("theme",)].file.name == "config.yaml"


def test_user_config_path_respects_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/x")
    assert user_config_path() == Path("/tmp/x/imp/config.yaml")


def test_test_cmd_fields_flow_from_project_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: project.yaml test_cmd_* land on Config, which the engine bundles
    # into TestGates / IMP_TEST_CMD_* for the gate subprocesses.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = tmp_path / "proj"
    (proj / "imp").mkdir(parents=True)
    (proj / "imp" / "project.yaml").write_text(
        "test_cmd_fast: make check\n"
        "test_cmd_slow: make check && make test-live\n"
        "test_cmd_fast_timeout: 120\n"
        "test_cmd_slow_timeout: 300\n")
    eff = load_effective_config(project_root=proj, control_root=None)
    assert eff.config.test_cmd_fast == "make check"
    assert eff.config.test_cmd_slow == "make check && make test-live"
    assert eff.config.test_cmd_fast_timeout == 120
    assert eff.config.test_cmd_slow_timeout == 300


def test_test_cmd_fields_default_to_none_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    eff = load_effective_config(project_root=None, control_root=None)
    assert eff.config.test_cmd_fast is None
    assert eff.config.test_cmd_slow is None
    assert eff.config.test_cmd_fast_timeout is None
    assert eff.config.test_cmd_slow_timeout is None


def test_repo_project_yaml_uses_tiered_test_cmds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shipped imp/project.yaml is migrated to the tiered keys; guard against a
    # regression to the removed verify_command/verify_timeout surface.
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(repo_root / "nonexistent-xdg"))
    eff = load_effective_config(project_root=repo_root, control_root=None)
    assert eff.config.test_cmd_fast == "make check"
    assert eff.config.test_cmd_slow == "make check && make test-live"
    assert eff.config.test_cmd_fast_timeout == 240
    assert eff.config.test_cmd_slow_timeout == 600
    raw = (repo_root / "imp" / "project.yaml").read_text()
    assert "verify_command" not in raw and "verify_timeout" not in raw
