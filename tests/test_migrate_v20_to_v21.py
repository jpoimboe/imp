from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v20_to_v21
from tests.conftest import _seed_control


def _seed_v20(control: Path, cid: str) -> Path:
    """A live home as the engine wrote it before this quest: stamped back to v20."""
    quest = Quest.create_draft(control, title="", quest_id=cid, base_branch="main")
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 20
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return quest.quest_dir


def _evidence(quest_dir: Path, rel: str, body: str) -> Path:
    d = quest_dir / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe.py").write_text(body)
    return d


def _meta(quest_dir: Path) -> dict:
    return json.loads((quest_dir / "quest.json").read_text())


def test_the_hop_renames_parked_evidence_to_the_sibling(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v20(tmp_path, "mv21a")
    _evidence(qd, "artifacts/spike", "print('x')\n")

    _migrate_v20_to_v21(qd)

    assert (qd / "spike" / "probe.py").read_text() == "print('x')\n"
    assert not (qd / "artifacts" / "spike").exists()
    assert _meta(qd)["layout_version"] == 21


def test_a_home_with_no_spike_evidence_only_stamps_the_version(tmp_path: Path) -> None:
    # The common case: most quests never dispatch a spiker.
    _seed_control(tmp_path)
    qd = _seed_v20(tmp_path, "mv21b")
    spec = qd / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / "feat.md").write_text("# spec\n")

    _migrate_v20_to_v21(qd)

    assert not (qd / "spike").exists()
    assert (qd / "artifacts" / "spec" / "feat.md").read_text() == "# spec\n"
    assert _meta(qd)["layout_version"] == 21


def test_both_directories_survive_when_both_exist(tmp_path: Path) -> None:
    # In-flight: an older run recreated artifacts/spike and still owns it.
    _seed_control(tmp_path)
    qd = _seed_v20(tmp_path, "mv21c")
    _evidence(qd, "artifacts/spike", "print('old')\n")
    _evidence(qd, "spike", "print('new')\n")

    _migrate_v20_to_v21(qd)

    assert (qd / "artifacts" / "spike" / "probe.py").read_text() == "print('old')\n"
    assert (qd / "spike" / "probe.py").read_text() == "print('new')\n"
    assert _meta(qd)["layout_version"] == 21


def test_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v20(tmp_path, "mv21d")
    _evidence(qd, "artifacts/spike", "print('x')\n")
    _migrate_v20_to_v21(qd)
    after_first = _meta(qd)

    _migrate_v20_to_v21(qd)

    assert _meta(qd) == after_first
    assert (qd / "spike" / "probe.py").read_text() == "print('x')\n"
    assert not (qd / "artifacts" / "spike").exists()


def test_raising_before_the_stamp_leaves_the_home_at_v20(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Work first, stamp last: a stamp-first hop would leave spike/ absent here.
    _seed_control(tmp_path)
    qd = _seed_v20(tmp_path, "mv21e")
    _evidence(qd, "artifacts/spike", "print('x')\n")

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("crash before the stamp")

    monkeypatch.setattr(quest_mod, "_write_quest_json", boom)
    with pytest.raises(RuntimeError):
        _migrate_v20_to_v21(qd)

    assert (qd / "spike" / "probe.py").read_text() == "print('x')\n"
    assert not (qd / "artifacts" / "spike").exists()
    assert _meta(qd)["layout_version"] == 20

    monkeypatch.undo()
    _migrate_v20_to_v21(qd)                    # the re-run completes the hop
    assert _meta(qd)["layout_version"] == 21


def test_load_migrates_a_live_home_through_the_chain(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    qd = _seed_v20(tmp_path, "mv21f")
    _evidence(qd, "artifacts/spike", "print('x')\n")

    Quest.load(tmp_path, "mv21f")              # triggers the migration chain

    assert (qd / "spike" / "probe.py").read_text() == "print('x')\n"
    assert _meta(qd)["layout_version"] == LAYOUT_VERSION


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 21
    assert quest_mod._MIGRATIONS[(20, 21)] is _migrate_v20_to_v21
