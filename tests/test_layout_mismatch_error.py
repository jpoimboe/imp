from __future__ import annotations

from pathlib import Path

from imp.engine.errors import LayoutMismatch, LoadError


def test_layout_mismatch_subclasses_loaderror() -> None:
    exc = LayoutMismatch(stored=13, engine=12, imp_commit="cafef00d",
                         quest_dir=Path("/ctl/.imp/quests/abc"))
    assert isinstance(exc, LoadError)


def test_message_names_quest_versions_and_update_advice() -> None:
    exc = LayoutMismatch(stored=13, engine=12, imp_commit="cafef00d",
                         quest_dir=Path("/ctl/.imp/quests/abc"))
    msg = str(exc)
    assert "abc" in msg                          # quest id (quest_dir.name), not the full path
    assert "/ctl/.imp/quests/abc" not in msg     # only quest_dir.name, not the full path
    assert "v13" in msg and "v12" in msg         # stored + engine versions
    assert "Update imp to v13 or newer" in msg   # advice points at updating imp
    assert "newer than this imp" in msg
    assert not msg.startswith("imp:")            # caller adds the imp: prefix


def test_message_omits_internal_breadcrumbs() -> None:
    """The corrected wording drops the old imp_commit / recover-recipe noise."""
    msg = str(LayoutMismatch(stored=13, engine=12, imp_commit="cafef00d",
                             quest_dir=Path("/ctl/.imp/quests/abc")))
    assert "cafef00d" not in msg
    assert "refusing to resume" not in msg


def test_below_current_message_names_an_older_home_without_update_advice() -> None:
    """stored < engine: an older imp wrote the home, so 'newer'/'update imp' is false."""
    msg = str(LayoutMismatch(stored=11, engine=23, imp_commit="cafef00d",
                             quest_dir=Path("/ctl/.imp/quests/abc")))
    assert "older than this imp" in msg
    assert "newer" not in msg
    assert "Update imp to v" not in msg
    assert "v11" in msg and "v23" in msg
    assert "\n" not in msg
