from __future__ import annotations

from datetime import UTC, datetime

from imp.cli.resume_select import Candidate, ResumePicker

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _cands() -> list[Candidate]:
    return [
        Candidate(change_id="2026-06-11-aaa111", title="Add version flag",
                  status="paused", step="plan", last_iso="2026-06-11T11:45:00+00:00"),
        Candidate(change_id="2026-06-10-bbb222", title="Other change",
                  status="errored", step="spec", last_iso="2026-06-10T11:45:00+00:00"),
    ]


def test_newest_is_preselected() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False)
    assert p.index == 0
    assert p.selected().change_id == "2026-06-11-aaa111"


def test_move_clamps_at_both_ends() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False)
    p.move_up()                                  # already top → clamp
    assert p.index == 0
    p.move_down()
    assert p.index == 1
    p.move_down()                                # already bottom → clamp
    assert p.index == 1
    assert p.selected().change_id == "2026-06-10-bbb222"


def test_render_marks_selection_and_uses_shared_formatter() -> None:
    lines = ResumePicker(_cands(), now=_NOW, color=False).render().splitlines()
    assert lines[0].startswith("▸ ")             # selected (newest) gets the marker
    assert lines[1].startswith("  ")             # unselected gets blank gutter
    assert "Add version flag" in lines[0]        # TITLE via change_row formatter
    assert "aaa111" in lines[0]                  # ID = resume token (modern-id suffix)
    assert "15m" in lines[0]                     # relative AGE (11:45 vs 12:00 _NOW)


def test_render_marker_moves_after_navigation() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False)
    p.move_down()
    lines = p.render().splitlines()
    assert lines[0].startswith("  ")             # first row no longer selected
    assert lines[1].startswith("▸ ")             # marker moved to second row
