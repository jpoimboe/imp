"""Tests for bundled_docs_dir() — imp's repo-root docs/ corpus resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

import imp.assets as assets
from imp.assets import bundled_docs_dir
from imp.paths import SOURCE_ROOT_MARKER


def test_bundled_docs_dir_resolves_the_repo_corpus() -> None:
    d = bundled_docs_dir()
    assert d.is_dir()
    assert d.name == "docs"
    # The corpus is non-empty markdown, read in place (survives the later reorg
    # into docs/guide and docs/internals — still *.md somewhere under docs/).
    assert any(d.rglob("*.md"))


def test_docs_dir_resolves_install_relative_not_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    # Simulate imp running on a non-imp project: cwd is an unrelated dir with
    # no docs/. The resolver is anchored to the install, not the cwd, so it
    # still finds imp's own corpus and a doc is readable in place from here.
    monkeypatch.chdir(tmp_path)
    d = bundled_docs_dir()
    assert d.is_dir()
    assert d != tmp_path / "docs"
    sample = next(d.rglob("*.md"))
    assert sample.read_text()


def test_docs_resolve_through_a_snapshots_source_root_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A session daemon runs from <quest home>/pinned-src/<token>/, whose parent
    # holds no docs/ — so the corpus is found through the snapshot's provenance
    # marker, naming the checkout it was copied from.
    checkout = tmp_path / "checkout"
    (checkout / "docs").mkdir(parents=True)
    (checkout / "docs" / "guide.md").write_text("# real corpus\n")
    snapshot = tmp_path / "quest" / "pinned-src" / "abc123"
    (snapshot / "imp" / "assets").mkdir(parents=True)
    (snapshot / SOURCE_ROOT_MARKER).write_text(f"{checkout / 'src'}\n")
    monkeypatch.setattr(
        assets, "files", lambda package: snapshot / "imp" / "assets")

    assert bundled_docs_dir() == checkout / "docs"
    assert (bundled_docs_dir() / "guide.md").read_text() == "# real corpus\n"
