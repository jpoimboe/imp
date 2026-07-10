from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _seed_control


def test_quest_home_and_meta_file_at_quests_path(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="que123", base_branch="main")
    assert ch.quest_dir == tmp_path / ".imp" / "quests" / "que123"
    assert (ch.quest_dir / "quest.json").exists()
    assert not (ch.quest_dir / "change.json").exists()
    assert not (tmp_path / ".imp" / "changes").exists()


def test_artifacts_guard_admits_quests_path() -> None:
    from imp.agents.tools.limits import leads_to_or_within_artifacts, within_artifacts
    assert within_artifacts(Path(".imp/quests/que123/artifacts/plans/p.md"))
    assert leads_to_or_within_artifacts(Path(".imp/quests"))
    assert not within_artifacts(Path(".imp/changes/que123/artifacts/plans/p.md"))


def test_rehydrate_recreates_home_under_quests(tmp_path: Path) -> None:
    # Guards the pathlib-join site read.py builds: rehydrate must place the
    # scratch home under .imp/quests (not .imp/changes) so a later Quest.load finds it.
    import inspect

    from imp.engine.archive import read as ar
    src = inspect.getsource(ar.rehydrate_quest)
    assert '"quests"' in src
    assert '"changes"' not in src
