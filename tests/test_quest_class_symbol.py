from pathlib import Path

from imp.engine.quest import Quest
from tests.conftest import _seed_control


def test_quest_class_creates_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="X", quest_id="que777", base_branch="main")
    assert q.id == "que777"
    assert q.base_branch == "main"
    assert q.quest_dir == tmp_path / ".imp" / "quests" / "que777"
    assert (q.quest_dir / "quest.json").exists()


def test_change_home_helpers_renamed_to_quest() -> None:
    import imp.cli.main as m
    for old in ("_change_home", "_list_change_homes", "_scan_change_homes"):
        assert not hasattr(m, old), f"{old} must be renamed to a _quest_* name"
    for new in ("_quest_home", "_list_quest_homes", "_scan_quest_homes"):
        assert callable(getattr(m, new, None)), f"{new} must exist and be callable"


def test_no_change_module_or_class() -> None:
    import importlib
    for name in ("imp.engine.change", "imp.cli.change_row"):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{name} should no longer exist")
