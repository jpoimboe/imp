from pathlib import Path


def test_imp_new_without_title_is_unnamed(tmp_path: Path) -> None:
    from imp.engine.change import Change
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="", quest_id="aaa000",
                       base_branch="main")
    assert ch.title == ""
