from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli import resume_select


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_render_shows_intent_for_untitled_candidate() -> None:
    c = resume_select.Candidate(
        change_id="c1", title="", status="draft", step="plan",
        last_iso="2026-06-01T00:00:00+00:00", intent="fix the flaky login test")
    picker = resume_select.ResumePicker(
        [c], now=datetime.now(UTC), color=False, header="pick")
    assert "fix the flaky login test" in picker.render()


@pytest.mark.anyio
async def test_pick_change_target_threads_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    row = ("c1", "draft", "plan", "2026-06-11T00:00:00+00:00", "",
           "2026-06-01T00:00:00+00:00", None)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_load_events_for_id",
        lambda control, cid: [
            {"type": "opening_intent", "payload": {"text": "add dark mode"}}])
    captured: dict[str, list] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["c"] = candidates
        return candidates[0].change_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    rc = await m._pick_change_target(
        control=tmp_path, rows=[row, row], header="pick",
        on_empty=lambda: 0, on_non_tty=None)
    assert rc == "c1"
    assert captured["c"][0].intent == "add dark mode"


@pytest.mark.anyio
async def test_pick_change_target_skips_event_load_for_titled_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    titled_row = ("t1", "draft", "plan", "2026-06-11T00:00:00+00:00", "Named feature",
                  "2026-06-01T00:00:00+00:00", None)
    untitled_row = ("u1", "draft", "plan", "2026-06-11T00:00:00+00:00", "",
                    "2026-06-01T00:00:00+00:00", None)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    called_with: list[str] = []

    def fake_load_events(control, cid):
        called_with.append(cid)
        return [{"type": "opening_intent", "payload": {"text": "add dark mode"}}]

    monkeypatch.setattr(m, "_load_events_for_id", fake_load_events)

    captured: dict[str, list] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["c"] = candidates
        return candidates[0].change_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)

    await m._pick_change_target(
        control=tmp_path,
        rows=[titled_row, untitled_row],
        header="pick",
        on_empty=lambda: 0,
        on_non_tty=None,
    )

    assert "t1" not in called_with, "titled row must not trigger event load"
    assert "u1" in called_with, "untitled row must trigger event load"

    candidates = captured["c"]
    titled = next(c for c in candidates if c.change_id == "t1")
    untitled = next(c for c in candidates if c.change_id == "u1")
    assert titled.intent is None
    assert untitled.intent == "add dark mode"
