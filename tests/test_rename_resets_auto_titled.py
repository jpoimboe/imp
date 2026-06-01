"""apply_pending_rename clears auto_titled (Plan 2, spec §12 / Plan 1 note):
once the spec phase has renamed a provisional slug, it is no longer auto-titled."""

from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from imp.engine.executor import apply_pending_rename
from imp.engine.state import State
from tests.conftest import _seed_control


def test_rename_clears_auto_titled(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="orig", slug="2026-06-01-prov-name",
                       base_branch="main", auto_titled=True)
    assert ch.auto_titled is True
    st = State.load(ch.state_dir)
    st.pending_rename = "Better Name"
    st.save(ch.state_dir)
    apply_pending_rename(ch, st, ch.event_log(), on_event=None)
    assert ch.slug == "2026-06-01-better-name"
    assert ch.auto_titled is False                 # in-memory reset
    assert Change.load(tmp_path, ch.slug).auto_titled is False   # persisted
