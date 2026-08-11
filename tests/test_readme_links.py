"""AC-10: every relative markdown link in README.md resolves to a git-tracked file."""

from __future__ import annotations

from pathlib import Path

from tests._corpus import relative_link_targets, tracked_paths


def _broken(text: str, tracked: set[str]) -> list[str]:
    return [t for t in relative_link_targets(text) if t not in tracked]


def test_readme_relative_links_resolve_to_tracked_files() -> None:
    text = Path("README.md").read_text()
    assert relative_link_targets(text), "README has no relative links to check"
    assert _broken(text, tracked_paths()) == []


def test_a_link_to_an_untracked_path_is_reported() -> None:
    text = (
        "See [gone](docs/guide/does-not-exist.md) and [here](README.md), "
        "plus [web](https://example.com/x.md) and [anchor](#install)."
    )
    assert _broken(text, tracked_paths()) == ["docs/guide/does-not-exist.md"]
