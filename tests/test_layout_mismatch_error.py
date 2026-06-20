from __future__ import annotations

from pathlib import Path

from imp.engine.errors import LayoutMismatch, LoadError


def test_layout_mismatch_subclasses_loaderror() -> None:
    exc = LayoutMismatch(stored=0, engine=1, imp_commit="cafef00d",
                         change_dir=Path("/ctl/.imp/changes/abc"))
    assert isinstance(exc, LoadError)


def test_message_carries_versions_commit_dir_and_recipe() -> None:
    exc = LayoutMismatch(stored=0, engine=3, imp_commit="cafef00d",
                         change_dir=Path("/ctl/.imp/changes/abc"))
    msg = str(exc)
    assert "0" in msg and "3" in msg                       # stored + engine versions
    assert "cafef00d" in msg                               # imp_commit breadcrumb
    assert "/ctl/.imp/changes/abc" in msg                  # change_dir
    assert "abandon" in msg                                # recovery recipe
    assert "imp log" in msg
    assert not msg.startswith("imp:")                      # caller adds the imp: prefix


def test_unknown_commit_renders_placeholder_not_empty() -> None:
    msg = str(LayoutMismatch(stored=0, engine=1, imp_commit="",
                             change_dir=Path("/ctl/.imp/changes/abc")))
    assert "(unknown)" in msg
