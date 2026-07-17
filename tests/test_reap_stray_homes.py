from __future__ import annotations

import json
from pathlib import Path

from imp.engine.archive.maintenance import reap_stray_homes


def _quests_root(control: Path) -> Path:
    root = control / ".imp" / "quests"
    root.mkdir(parents=True)
    return root


def test_reaps_empty_stray_home(tmp_path: Path) -> None:
    root = _quests_root(tmp_path)
    stray = root / "stray"
    stray.mkdir()                                  # empty: no quest.json, no entries

    assert reap_stray_homes(tmp_path) == 1
    assert not stray.exists()                       # empty stray removed


def test_keeps_live_home_with_quest_json(tmp_path: Path) -> None:
    root = _quests_root(tmp_path)
    live = root / "live"
    live.mkdir()
    (live / "quest.json").write_text(json.dumps({"id": "live"}))

    assert reap_stray_homes(tmp_path) == 0
    assert live.exists()                            # home with quest.json kept


def test_keeps_nonempty_home_without_quest_json(tmp_path: Path) -> None:
    root = _quests_root(tmp_path)
    partial = root / "partial"
    (partial / "state").mkdir(parents=True)         # non-empty, no quest.json

    assert reap_stray_homes(tmp_path) == 0          # rmdir refuses non-empty; swallowed
    assert partial.exists()                         # lone state/ subdir keeps it alive


def test_noop_when_quests_root_absent(tmp_path: Path) -> None:
    assert reap_stray_homes(tmp_path) == 0          # no .imp/quests/ at all


def test_reaps_only_the_empty_strays_in_a_mixed_root(tmp_path: Path) -> None:
    root = _quests_root(tmp_path)
    (root / "s1").mkdir()                            # empty stray
    (root / "s2").mkdir()                            # empty stray
    live = root / "live"
    live.mkdir()
    (live / "quest.json").write_text("{}")

    assert reap_stray_homes(tmp_path) == 2          # both strays, not the live home
    assert not (root / "s1").exists()
    assert not (root / "s2").exists()
    assert live.exists()
