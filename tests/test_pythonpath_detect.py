from __future__ import annotations

from pathlib import Path

from imp.cli.pythonpath_detect import (
    detect_pythonpath,
    format_pythonpath_yaml,
    resolve_pythonpath_choice,
)


def _seq(*answers: str):
    it = iter(answers)
    return lambda _prompt="": next(it)


# --- detect_pythonpath heuristic ---

def test_detect_src_layout_returns_src(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    assert detect_pythonpath(tmp_path) == ["src"]


def test_detect_flat_pyproject_returns_dot(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_pythonpath(tmp_path) == ["."]


def test_detect_none_when_neither(tmp_path: Path) -> None:
    assert detect_pythonpath(tmp_path) is None


def test_detect_src_layout_wins_over_pyproject(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_pythonpath(tmp_path) == ["src"]


def test_detect_bare_src_dir_without_package_falls_through_to_pyproject(
    tmp_path: Path,
) -> None:
    # A `src/` with no `*/__init__.py` is not a src-layout package; a root
    # pyproject then wins the flat-layout branch.
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_pythonpath(tmp_path) == ["."]


# --- resolve_pythonpath_choice accept / edit / skip ---

def test_resolve_accept_returns_suggested() -> None:
    assert resolve_pythonpath_choice(
        ["src"], _seq("y"), lambda _m: None) == ["src"]


def test_resolve_blank_answer_accepts() -> None:
    assert resolve_pythonpath_choice(
        ["src"], _seq(""), lambda _m: None) == ["src"]


def test_resolve_edit_returns_entered_dirs() -> None:
    # edit: first answer selects edit, second supplies space-separated dirs.
    assert resolve_pythonpath_choice(
        ["src"], _seq("edit", "lib vendor"), lambda _m: None) == ["lib", "vendor"]


def test_resolve_edit_blank_falls_back_to_suggested() -> None:
    assert resolve_pythonpath_choice(
        ["src"], _seq("edit", "   "), lambda _m: None) == ["src"]


def test_resolve_skip_returns_none() -> None:
    assert resolve_pythonpath_choice(
        ["src"], _seq("skip"), lambda _m: None) is None


def test_resolve_skip_aliases_opt_out() -> None:
    for alias in ("s", "n", "no"):
        assert resolve_pythonpath_choice(
            ["src"], _seq(alias), lambda _m: None) is None


def test_resolve_unrecognized_defaults_to_suggested() -> None:
    assert resolve_pythonpath_choice(
        ["src"], _seq("wat"), lambda _m: None) == ["src"]


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
