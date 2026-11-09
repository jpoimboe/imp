"""Tests for bundled_docs_dir() — imp's docs corpus resolver."""

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


def test_a_packaged_corpus_wins_over_a_sibling_docs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wheel install: the corpus ships inside the package as imp/assets/docs.
    root = tmp_path / "site-packages"
    packaged = root / "imp" / "assets" / "docs"
    packaged.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(assets, "files", lambda package: root / "imp" / "assets")

    assert bundled_docs_dir() == packaged


def test_a_missing_corpus_names_both_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "site-packages"
    (root / "imp" / "assets").mkdir(parents=True)
    monkeypatch.setattr(assets, "files", lambda package: root / "imp" / "assets")

    with pytest.raises(RuntimeError) as exc:
        bundled_docs_dir()
    assert str(exc.value) == (
        f"imp docs corpus not found at {root / 'imp' / 'assets' / 'docs'} or "
        f"{tmp_path / 'docs'}; the imp install is incomplete — reinstall imp"
    )
