from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import imp.cli.main as m
import imp.cli.store as s
from imp.cli import resume_select


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_render_shows_intent_for_untitled_candidate() -> None:
    c = resume_select.Candidate(
        quest_id="c1", title="", status="draft", step="plan",
        last_iso="2026-06-01T00:00:00+00:00", intent="fix the flaky login test")
    picker = resume_select.ResumePicker(
        [c], now=datetime.now(UTC), color=False, header="pick")
    # untitled candidate falls back to its opening prompt, unprefixed
    rendered = picker.render()
    assert "fix the flaky login" in rendered
    assert "> fix the flaky login" not in rendered


@pytest.mark.anyio
async def test_pick_quest_target_threads_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row = ("c1", "draft", "plan", "2026-06-11T00:00:00+00:00", "",
           "2026-06-01T00:00:00+00:00", None, None)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        s, "_load_events_for_id",
        lambda control, cid: [
            {"type": "user_message", "payload": {"subtype": "intent",
             "content": [{"type": "text", "text": "add dark mode"}]}}])
    captured: dict[str, list] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["c"] = candidates
        return candidates[0].quest_id

    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)
    rc = await m._pick_quest_target(
        control=tmp_path, rows=[row, row], header="pick",
        on_empty=lambda: 0, on_cancel=lambda: 0, on_non_tty=None)
    assert rc == "c1"
    assert captured["c"][0].intent == "add dark mode"


@pytest.mark.anyio
async def test_pick_quest_target_skips_event_load_for_titled_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    titled_row = ("t1", "draft", "plan", "2026-06-11T00:00:00+00:00", "Named feature",
                  "2026-06-01T00:00:00+00:00", None, None)
    untitled_row = ("u1", "draft", "plan", "2026-06-11T00:00:00+00:00", "",
                    "2026-06-01T00:00:00+00:00", None, None)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    called_with: list[str] = []

    def fake_load_events(control, cid):
        called_with.append(cid)
        return [{"type": "user_message", "payload": {"subtype": "intent",
                 "content": [{"type": "text", "text": "add dark mode"}]}}]

    monkeypatch.setattr(s, "_load_events_for_id", fake_load_events)

    captured: dict[str, list] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["c"] = candidates
        return candidates[0].quest_id

    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)

    await m._pick_quest_target(
        control=tmp_path,
        rows=[titled_row, untitled_row],
        header="pick",
        on_empty=lambda: 0,
        on_cancel=lambda: 0,
        on_non_tty=None,
    )

    assert "t1" not in called_with, "titled row must not trigger event load"
    assert "u1" in called_with, "untitled row must trigger event load"

    candidates = captured["c"]
    titled = next(c for c in candidates if c.quest_id == "t1")
    untitled = next(c for c in candidates if c.quest_id == "u1")
    assert titled.intent is None
    assert untitled.intent == "add dark mode"


def _two_resumables(control: Path) -> str:
    from imp.create import create_draft_quest
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control
    _seed_control(control)
    draft = create_draft_quest(control, "summarize\n  foo.py")
    Quest.create_draft(control, title="Other work", quest_id="oth001",
                       base_branch="main", origin="idea")
    return draft.id


@pytest.mark.anyio
async def test_non_tty_resume_candidates_name_an_unstarted_draft_by_its_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    qid = _two_resumables(tmp_path)
    rc = await m._resolve_resume_target(tmp_path, None, non_interactive=True)
    assert rc == 2
    err = capsys.readouterr().err
    row = next(ln for ln in err.splitlines() if ln.startswith(qid))
    assert "summarize foo.py" in row
    assert "(unnamed)" not in err


@pytest.mark.anyio
async def test_tty_resume_picker_names_an_unstarted_draft_by_its_seed_opening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    qid = _two_resumables(tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    captured: dict[str, list] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["c"] = candidates
        return candidates[0].quest_id

    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)
    await m._pick_resume_target(tmp_path, non_interactive=False)
    by_id = {c.quest_id: c for c in captured["c"]}
    assert by_id[qid].intent == "summarize\n  foo.py"
    assert by_id["oth001"].intent is None
