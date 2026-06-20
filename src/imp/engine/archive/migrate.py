"""One-time importer from the legacy refs/imp/journal ref into the archive."""

from __future__ import annotations

import tempfile
from pathlib import Path

from imp.engine import archive, journal

_RUNTIME_FILES = ("events.jsonl", "change.json", "state.json")


def import_change_runtime(control_root: Path, change_id: str) -> bool:
    """Reconstruct one journaled Change's record + events blob + index rows from
    refs/imp/journal:<id>. Returns False when the Change's change.json/state.json
    are absent from the journal (nothing to import)."""
    change_json = journal.journal_show(control_root, change_id, "change.json")
    state_json = journal.journal_show(control_root, change_id, "state.json")
    if change_json is None or state_json is None:
        return False
    events = journal.journal_show(control_root, change_id, "events.jsonl") or b""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "events.jsonl").write_bytes(events)
        (tmp / "change.json").write_bytes(change_json)
        (tmp / "state.json").write_bytes(state_json)
        archive.archive_change(
            control_root, change_id,
            events_path=tmp / "events.jsonl",
            change_json_path=tmp / "change.json",
            state_json_path=tmp / "state.json")
    return True
