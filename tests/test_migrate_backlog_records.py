from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from imp.engine.change import Change
from tests.conftest import _seed_control


def _load_migrate():
    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_backlog_records.py"
    spec = importlib.util.spec_from_file_location("migrate_backlog_records", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_record(control: Path, change_id: str, **fields) -> None:
    d = control / ".imp" / "backlog"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"id": change_id, "title": "T", "description": "",
           "depends_on": [], "created_by": "human", "workflow": None}
    rec.update(fields)
    (d / f"{change_id}.json").write_text(json.dumps(rec))


def test_migrate_followup_record_becomes_change_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _write_record(tmp_path, "rec0001", title="Fix drift",
                  description="reconcile the allowlist",
                  depends_on=["dep0001"], created_by="step:implement/finalize")
    created = _load_migrate().migrate(tmp_path)
    assert created == ["rec0001"]
    change = Change.load(tmp_path, "rec0001")
    assert change.materialized is False
    assert change.origin == "followup"
    assert change.title == "Fix drift"
    assert change.seed_opening == "Fix drift\n\nreconcile the allowlist"
    meta = json.loads((change.change_dir / "change.json").read_text())
    assert meta["depends_on"] == ["dep0001"]


def test_migrate_human_record_is_idea_origin(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _write_record(tmp_path, "rec0002", created_by="human")
    _load_migrate().migrate(tmp_path)
    assert Change.load(tmp_path, "rec0002").origin == "idea"


def test_migrate_removes_backlog_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _write_record(tmp_path, "rec0003")
    _load_migrate().migrate(tmp_path)
    assert not (tmp_path / ".imp" / "backlog").exists()


def test_migrate_is_idempotent_skips_existing_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Already", change_id="rec0004",
                        base_branch="main", origin="idea", seed_opening="Already")
    _write_record(tmp_path, "rec0004", title="Stale record")
    created = _load_migrate().migrate(tmp_path)
    assert created == []                                        # existing home skipped
    assert Change.load(tmp_path, "rec0004").title == "Already"  # home untouched
    assert not (tmp_path / ".imp" / "backlog").exists()         # dir removed even when all skipped


def test_migrate_no_backlog_dir_is_noop(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert _load_migrate().migrate(tmp_path) == []
