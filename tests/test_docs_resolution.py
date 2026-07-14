"""Tests for bundled_docs_dir() — imp's repo-root docs/ corpus resolver."""

from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_docs_dir


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
