"""Worktree-setup detection: the lockfile table, its order, and its YAML spelling."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.setup_detect import (
    DetectedSetupCommand,
    detect_worktree_setup,
    format_worktree_setup_yaml,
)


def test_none_when_no_table_lockfile_is_present(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    assert detect_worktree_setup(tmp_path) is None


def test_a_shared_cache_toolchain_is_deliberately_not_in_the_table(tmp_path: Path) -> None:
    # Cargo's crate cache is shared, not per-tree: a Rust repo must detect nothing.
    (tmp_path / "Cargo.lock").write_text("version = 3\n")
    assert detect_worktree_setup(tmp_path) is None


@pytest.mark.parametrize(("lockfile", "command"), [
    ("uv.lock", "uv sync --all-extras"),
    ("poetry.lock", "poetry install"),
    ("package-lock.json", "npm ci"),
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn install"),
    ("Gemfile.lock", "bundle install"),
    ("mix.lock", "mix deps.get"),
    ("composer.lock", "composer install"),
])
def test_each_lockfile_maps_to_its_command_and_provenance(
    tmp_path: Path, lockfile: str, command: str,
) -> None:
    (tmp_path / lockfile).write_text("")
    assert detect_worktree_setup(tmp_path) == DetectedSetupCommand(
        command, f"from {lockfile}")


def test_first_match_wins_in_table_order(tmp_path: Path) -> None:
    for name in ("composer.lock", "package-lock.json", "uv.lock"):
        (tmp_path / name).write_text("")
    assert detect_worktree_setup(tmp_path) == DetectedSetupCommand(
        "uv sync --all-extras", "from uv.lock")


def test_first_match_wins_between_two_node_lockfiles(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_worktree_setup(tmp_path) == DetectedSetupCommand(
        "pnpm install --frozen-lockfile", "from pnpm-lock.yaml")


def test_format_spells_both_states_yaml_accepts() -> None:
    assert format_worktree_setup_yaml("uv sync --all-extras") == (
        'worktree_setup: "uv sync --all-extras"\n')
    assert format_worktree_setup_yaml(False) == "worktree_setup: false\n"
