"""apply_pending_rename preserves the YYYY-MM-DD- date prefix across a rename
(spec §12). A bare (date-less) slug falls back to plain slugify."""

from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from imp.engine.executor import apply_pending_rename
from imp.engine.state import State
from tests.conftest import _seed_control


def _rename(tmp_path: Path, *, slug: str, new_name: str) -> Change:
    ch = Change.create(tmp_path, title="orig", slug=slug, base_branch="main")
    st = State.load(ch.state_dir)
    st.pending_rename = new_name
    st.save(ch.state_dir)
    apply_pending_rename(ch, st, ch.event_log(), on_event=None)
    return ch


def test_rename_preserves_date_prefix(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _rename(tmp_path, slug="2026-06-01-old-name", new_name="Better Name")
    assert ch.slug == "2026-06-01-better-name"
    assert ch.title == "Better Name"


def test_rename_without_date_prefix_uses_plain_slugify(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _rename(tmp_path, slug="old-name", new_name="Better Name")
    assert ch.slug == "better-name"
