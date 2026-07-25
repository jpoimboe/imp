from __future__ import annotations

from pathlib import Path

from imp.cli.pythonpath_detect import format_pythonpath_yaml

# --- format_pythonpath_yaml serializer ---

def test_format_single_root() -> None:
    assert format_pythonpath_yaml(["src"]) == 'pythonpath: ["src"]\n'


def test_format_multi_root() -> None:
    assert format_pythonpath_yaml(["src", "."]) == 'pythonpath: ["src", "."]\n'


from imp.cli.pythonpath_detect import (  # noqa: E402
    is_python_packaging_project,
    resolve_detection_provider,
    validate_detected_roots,
)
from imp.providers.config import Config  # noqa: E402
from imp.providers.effective import EffectiveConfig  # noqa: E402


def test_is_python_packaging_project_detects_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert is_python_packaging_project(tmp_path) is True


def test_is_python_packaging_project_detects_setup_cfg(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n")
    assert is_python_packaging_project(tmp_path) is True


def test_is_python_packaging_project_detects_setup_py(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    assert is_python_packaging_project(tmp_path) is True


def test_is_python_packaging_project_false_for_bare_dir(tmp_path: Path) -> None:
    assert is_python_packaging_project(tmp_path) is False


def test_resolve_detection_provider_picks_first_usable(
    monkeypatch, provider_registry
) -> None:
    provider_registry("first", env_check=lambda: True, factory=lambda m: None)
    provider_registry("second", env_check=lambda: True, factory=lambda m: None)
    cfg = Config(providers=(), tiers={"medium": {"first": "m-first", "second": "m-second"}})
    import imp.cli.pythonpath_detect as pp
    monkeypatch.setattr(
        pp, "load_effective_config",
        lambda: EffectiveConfig(config=cfg, provenance={}))
    # Both pass env_check; the first listed provider must win over the second.
    assert resolve_detection_provider(["first", "second"]) == ("first", "m-first")


def test_resolve_detection_provider_none_when_no_usable(
    monkeypatch, provider_registry
) -> None:
    provider_registry("nocred", env_check=lambda: False, factory=lambda m: None)
    cfg = Config(providers=(), tiers={"medium": {"nocred": "m2"}})
    import imp.cli.pythonpath_detect as pp
    monkeypatch.setattr(
        pp, "load_effective_config",
        lambda: EffectiveConfig(config=cfg, provenance={}))
    assert resolve_detection_provider(["nocred"]) is None



def test_validate_keeps_existing_dirs_and_normalizes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "lib").mkdir()
    assert validate_detected_roots(tmp_path, ["./src", "lib/"]) == ["src", "lib"]


def test_validate_normalizes_root_markers_to_dot(tmp_path: Path) -> None:
    assert validate_detected_roots(tmp_path, ["", "./", ".", ".//"]) == ["."]


def test_validate_drops_missing_absolute_and_escaping_entries(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    result = validate_detected_roots(
        tmp_path, ["src", "nope", "/etc", "../outside", "a/../b"])
    assert result == ["src"]


def test_validate_deduplicates(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert validate_detected_roots(tmp_path, ["src", "./src", "src/"]) == ["src"]


def test_validate_empty_input_returns_empty(tmp_path: Path) -> None:
    assert validate_detected_roots(tmp_path, []) == []


def test_detector_and_agent_are_retired() -> None:
    import imp.cli.pythonpath_detect as ppd
    from imp.assets import bundled_workflow_dir
    # The one-shot detector is gone; its validation/format helpers remain.
    assert not hasattr(ppd, "detect_pythonpath_via_agent")
    assert hasattr(ppd, "validate_detected_roots")
    assert hasattr(ppd, "resolve_detection_provider")
    # The bundled agent file is gone.
    assert not (bundled_workflow_dir("default") / "agents"
                / "init-pythonpath.md").exists()
