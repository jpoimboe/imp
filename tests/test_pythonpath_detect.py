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
