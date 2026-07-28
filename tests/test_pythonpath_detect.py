from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.pythonpath_detect import format_pythonpath_yaml

# --- format_pythonpath_yaml serializer ---

def test_format_single_root() -> None:
    assert format_pythonpath_yaml(["src"]) == 'pythonpath: ["src"]\n'


def test_format_multi_root() -> None:
    assert format_pythonpath_yaml(["src", "."]) == 'pythonpath: ["src", "."]\n'


from imp.cli.pythonpath_detect import (  # noqa: E402
    ImportRoots,
    detect_import_roots,
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
    assert resolve_detection_provider(["first", "second"], tier="medium") == (
        "first", "m-first")


def test_resolve_detection_provider_none_when_no_usable(
    monkeypatch, provider_registry
) -> None:
    provider_registry("nocred", env_check=lambda: False, factory=lambda m: None)
    cfg = Config(providers=(), tiers={"medium": {"nocred": "m2"}})
    import imp.cli.pythonpath_detect as pp
    monkeypatch.setattr(
        pp, "load_effective_config",
        lambda: EffectiveConfig(config=cfg, provenance={}))
    assert resolve_detection_provider(["nocred"], tier="medium") is None


def test_resolve_detection_provider_requires_an_explicit_tier() -> None:
    """No default tier: a caller must name the one it means, so init's
    declared tier can never silently drift back to medium."""
    with pytest.raises(TypeError, match="tier"):
        resolve_detection_provider(["first"])


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


# --- detect_import_roots: gate + disk probe ---

def test_import_roots_empty_for_non_python_project(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_text("// not python\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    # No pyproject/setup.cfg/setup.py: the packaging gate short-circuits even
    # though a package dir sits right there.
    assert detect_import_roots(tmp_path) == ImportRoots([], "not a Python project")


def test_import_roots_probe_src_layout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["src"], "from the layout on disk")


def test_import_roots_probe_flat_layout(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["."], "from the layout on disk")


def test_import_roots_empty_when_no_package_dir_anywhere(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "src").mkdir()          # exists, but holds no package
    (tmp_path / "docs").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots([], "not a Python project")


# --- detect_import_roots: declared paths in pyproject.toml ---

def test_import_roots_hatch_packages(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["lib/app", "lib/other"]\n')
    (tmp_path / "lib" / "app").mkdir(parents=True)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    # The declaration wins over the src-layout the probe would otherwise find,
    # and the two declared packages collapse to their single parent dir.
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["lib"], "from pyproject.toml")


def test_import_roots_setuptools_package_dir_root_key(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools]\npackage-dir = {"" = "source"}\n')
    (tmp_path / "source").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["source"], "from pyproject.toml")


def test_import_roots_setuptools_package_dir_named_package(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools]\npackage-dir = {mypkg = "lib/mypkg"}\n')
    (tmp_path / "lib" / "mypkg").mkdir(parents=True)
    # A named package maps to its OWN directory, so the import root is its
    # parent — `lib`, never `lib/mypkg` (which would be unimportable).
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["lib"], "from pyproject.toml")


def test_import_roots_setuptools_packages(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools]\npackages = ["lib/app"]\n')
    (tmp_path / "lib").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["lib"], "from pyproject.toml")


def test_import_roots_poetry_packages_from(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\npackages = [{include = "app", from = "lib"}]\n')
    (tmp_path / "lib").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["lib"], "from pyproject.toml")


def test_import_roots_pytest_pythonpath(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["stubs"]\n')
    (tmp_path / "stubs").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["stubs"], "from pyproject.toml")


def test_import_roots_empty_declaration_does_not_mask_a_later_key(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.hatch.build.targets.wheel]\npackages = []\n\n"
        '[tool.pytest.ini_options]\npythonpath = ["stubs"]\n')
    (tmp_path / "stubs").mkdir()
    # An empty `packages` declares nothing, so the next key in the same file
    # still gets its say.
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["stubs"], "from pyproject.toml")


def test_import_roots_declared_missing_dir_falls_through_to_probe(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel]\npackages = ["ghost/app"]\n')
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    # `ghost` is not on disk, so validation empties the declaration and the
    # probe answers instead — never a declared root that doesn't exist.
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["src"], "from the layout on disk")


def test_import_roots_unparseable_pyproject_falls_through_to_probe(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.hatch\nthis is not = = toml\n")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["src"], "from the layout on disk")


# --- detect_import_roots: declared paths in setup.cfg / setup.py ---

def test_import_roots_setup_cfg_package_dir_root_key(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n[options]\npackage_dir =\n    = lib\n")
    (tmp_path / "lib").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(["lib"], "from setup.cfg")


def test_import_roots_setup_cfg_package_dir_named_package(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n[options]\npackage_dir =\n    mypkg = lib/mypkg\n")
    (tmp_path / "lib" / "mypkg").mkdir(parents=True)
    assert detect_import_roots(tmp_path) == ImportRoots(["lib"], "from setup.cfg")


def test_import_roots_setup_cfg_pytest_pythonpath(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n[tool:pytest]\npythonpath = stubs\n")
    (tmp_path / "stubs").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(["stubs"], "from setup.cfg")


def test_import_roots_setup_py_package_dir(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='x', package_dir={'': 'lib'})\n")
    (tmp_path / "lib").mkdir()
    assert detect_import_roots(tmp_path) == ImportRoots(["lib"], "from setup.py")


def test_import_roots_setup_py_computed_value_falls_through_to_probe(
    tmp_path: Path,
) -> None:
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "def where():\n    return {'': 'lib'}\n"
        "setup(name='x', package_dir=where())\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    # Not a literal, and setup.py is never executed — the probe answers.
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["src"], "from the layout on disk")


def test_import_roots_pyproject_wins_over_setup_cfg(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools]\npackage-dir = {"" = "declared"}\n')
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n[options]\npackage_dir =\n    = other\n")
    (tmp_path / "declared").mkdir()
    (tmp_path / "other").mkdir()
    # Both sources declare existing directories; pyproject.toml must take priority.
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["declared"], "from pyproject.toml")


def test_import_roots_setup_cfg_package_dir_wins_over_pytest_pythonpath(
    tmp_path: Path,
) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nname = x\n\n[options]\npackage_dir =\n    = lib\n\n"
        "[tool:pytest]\npythonpath = stubs\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "stubs").mkdir()
    # Within a single setup.cfg, package_dir must win over [tool:pytest] pythonpath.
    assert detect_import_roots(tmp_path) == ImportRoots(["lib"], "from setup.cfg")


def test_import_roots_unparseable_setup_cfg_falls_through_to_probe(
    tmp_path: Path,
) -> None:
    # A key before any section header triggers MissingSectionHeaderError.
    (tmp_path / "setup.cfg").write_text(
        "key = value without section header\n[options]\npackage_dir =\n    = lib\n")
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    assert detect_import_roots(tmp_path) == ImportRoots(
        ["src"], "from the layout on disk")


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
