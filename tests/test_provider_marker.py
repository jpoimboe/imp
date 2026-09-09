"""The pending-switch marker: set by the server where the frame is handled,
cleared by the engine at the reconcile, rendered dim on the activity line
(Scope 3, 7; AC 8e, 8f, 11b(c), 18b)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.cli import narrative as nv
from imp.cli.live_driver import compose_activity_line
from imp.engine.executor import _reconcile_provider
from imp.remote.session.protocol import CONTROL_SET_PROVIDER
from imp.remote.session.server import SessionServer
from tests.test_provider_reconcile import _raise_load_error, _seed

_CLEAR = {"type": "provider_marker", "payload": {"provider": None}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_the_server_broadcasts_the_marker_and_journals_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    server = SessionServer(str(tmp_path / "s.sock"), events_path=events_path,
                           set_provider=lambda _n: None)
    broadcasts: list[dict[str, Any]] = []
    monkeypatch.setattr(server._socket, "broadcast", broadcasts.append)

    await server._dispatch_control(CONTROL_SET_PROVIDER, "codex")

    assert broadcasts == [{"type": "provider_marker",
                           "payload": {"provider": "codex"}}]
    # Transient: a later attach must not replay a marker already reconciled.
    assert events_path.read_text() == ""


def test_the_engine_clears_the_marker_before_it_confirms(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    quest.set_provider("codex")
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(quest, state, workflow, events.append) is True

    assert events[0] == _CLEAR
    assert events[1]["type"] == "script_output"


def test_a_refused_switch_also_clears_the_marker(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    # AC 11b(c): the run stays on its vendor, so nothing later would clear it.
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    quest.set_provider("codex")
    _raise_load_error(monkeypatch)
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(quest, state, workflow, events.append) is False
    assert events[0] == _CLEAR


def test_an_unswitched_run_broadcasts_no_marker_traffic(
    tmp_path: Path, floor_availability,
) -> None:
    quest, state, workflow = _seed(tmp_path)
    floor_availability({"claude_code", "codex"})
    events: list[dict[str, Any]] = []

    assert _reconcile_provider(quest, state, workflow, events.append) is False
    assert events == []


def test_the_activity_line_carries_the_verbatim_marker() -> None:
    activity = nv.Activity(kind="agent", verb="thinking")
    line = compose_activity_line(activity=activity, frame=0, color=False,
                                 now_iso="2026-01-01T00:00:00+00:00",
                                 pending_provider="codex")
    assert "switching to codex after this step" in line
    plain = compose_activity_line(activity=activity, frame=0, color=False,
                                  now_iso="2026-01-01T00:00:00+00:00")
    assert "switching to" not in plain


def test_the_marker_renders_dim() -> None:
    line = compose_activity_line(
        activity=nv.Activity(kind="agent", verb="thinking"), frame=0,
        color=True, now_iso="2026-01-01T00:00:00+00:00",
        pending_provider="codex")
    assert "\x1b[2mswitching to codex after this step" in line
