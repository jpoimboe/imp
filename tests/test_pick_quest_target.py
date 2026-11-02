from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# _Row = (id, status, step, last_iso, title, created_at, step_label, phase_label)
def _row(
    cid: str, status: str = "paused",
    last: str = "2026-06-11T00:00:00+00:00",
    step_label: str | None = None,
    phase_label: str | None = None,
) -> tuple[str, str, str, str, str, str, str | None, str | None]:
    return (cid, status, "plan", last, "Title " + cid,
            "2026-06-01T00:00:00+00:00", step_label, phase_label)


@pytest.mark.anyio
async def test_empty_routes_to_on_empty() -> None:
    called = {}

    def on_empty() -> int:
        called["empty"] = True
        return 7

    rc = await m._pick_quest_target(
        control=Path("."), rows=[], header="h",
        on_empty=on_empty, on_cancel=lambda: 0, on_non_tty=lambda rows: 0)
    assert rc == 7
    assert called["empty"]


@pytest.mark.anyio
async def test_non_tty_routes_to_on_non_tty(monkeypatch) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: False)
    seen = {}

    def on_non_tty(rows) -> int:
        seen["rows"] = rows
        return 9

    rc = await m._pick_quest_target(
        control=Path("."), rows=[_row("a")], header="h",
        on_empty=lambda: 0, on_cancel=lambda: 0, on_non_tty=on_non_tty)
    assert rc == 9
    assert seen["rows"][0][0] == "a"


@pytest.mark.anyio
async def test_non_interactive_flag_routes_to_on_non_tty(monkeypatch) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: True)
    seen = {}

    def on_non_tty(rows) -> int:
        seen["rows"] = rows
        return 9

    rc = await m._pick_quest_target(
        control=Path("."), rows=[_row("a")], header="h",
        on_empty=lambda: 0, on_cancel=lambda: 0, on_non_tty=on_non_tty,
        non_interactive=True)
    assert rc == 9
    assert seen["rows"][0][0] == "a"


@pytest.mark.anyio
async def test_tty_lone_auto_selects_without_drawing_picker(monkeypatch) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def boom(*a, **k):
        raise AssertionError("picker drawn for a lone candidate")

    monkeypatch.setattr("imp.cli.resume_select.run_picker", boom)
    rc = await m._pick_quest_target(
        control=Path("."), rows=[_row("solo")], header="h",
        on_empty=lambda: 0, on_cancel=lambda: 0, on_non_tty=lambda rows: 0)
    assert rc == "solo"


@pytest.mark.anyio
async def test_tty_multiple_runs_picker_with_header_and_index(monkeypatch) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: True)
    captured = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["header"] = header
        captured["selected_index"] = selected_index
        return candidates[selected_index].quest_id

    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)
    rc = await m._pick_quest_target(
        control=Path("."), rows=[_row("a"), _row("b")], header="Pick one?",
        on_empty=lambda: 0, on_cancel=lambda: 0, on_non_tty=lambda rows: 0, selected_index=1)
    assert rc == "b"
    assert captured["header"] == "Pick one?"
    assert captured["selected_index"] == 1


@pytest.mark.anyio
async def test_pick_quest_target_threads_phase_and_step_label(monkeypatch) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: True)
    captured: dict = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["candidates"] = candidates
        return candidates[0].quest_id

    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)
    rows = [
        _row("x", step_label="coding", phase_label="implement phase"),
        _row("y", step_label="testing", phase_label="verify phase"),
    ]
    await m._pick_quest_target(
        control=Path("."), rows=rows, header="h",
        on_empty=lambda: 0, on_cancel=lambda: 0, on_non_tty=lambda rows: 0)
    assert captured["candidates"][0].phase_label == "implement phase"
    assert captured["candidates"][0].step_label == "coding"
    assert captured["candidates"][1].phase_label == "verify phase"
    assert captured["candidates"][1].step_label == "testing"


@pytest.mark.anyio
async def test_picker_cancel_routes_to_on_cancel(monkeypatch) -> None:
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(candidates, *, now, color, header, selected_index):
        return None

    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)
    rc = await m._pick_quest_target(
        control=Path("."), rows=[_row("a"), _row("b")], header="h",
        on_empty=lambda: 0, on_cancel=lambda: 11, on_non_tty=lambda rows: 0)
    assert rc == 11
