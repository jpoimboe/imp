"""AC 11: coord.interrupted remaps exactly the non-zero, non-completed rows of the
closing block to 130, and leaves every other row's code alone."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.cli.session_run import run_session_client
from imp.cli.signals import InterruptCoordinator
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.client import StreamEnding
from tests.test_session_run import (
    _abandoning,
    _cut,
    _ended,
    _ending_with,
    _leaving,
    _make_quest,
    _never,
    _NullDriver,
    _watched,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(("client", "status", "expected"), [
    pytest.param(_abandoning, "paused", 0, id="row1-abandon"),
    pytest.param(
        _ending_with(StreamEnding.CUT,
                     {"type": "quest_merged", "payload": {"base": "main"}}),
        "paused", 0, id="row2-merged"),
    pytest.param(
        _ending_with(StreamEnding.CUT, {"type": "workflow_completed", "payload": {}}),
        "paused", 0, id="row3-completed"),
    pytest.param(
        _ending_with(StreamEnding.CUT,
                     {"type": "merge_blocked", "payload": {"reason": "conflict"}}),
        "paused", 130, id="row4-merge-blocked"),
    pytest.param(_leaving("ctrl-d"), "paused", 130, id="row5-leave-trigger"),
    pytest.param(
        _ending_with(StreamEnding.CUT,
                     {"type": "workflow_paused", "payload": {"reason": "max_loops"}}),
        "paused", 130, id="row6-witnessed-pause"),
    pytest.param(_leaving(None), "paused", 130, id="row7-bare-departure"),
    pytest.param(_cut, "merged", 0, id="row8a-disk-completed"),
    pytest.param(_cut, None, 4, id="row8b-lost-connection"),
    pytest.param(_ended, "paused", 130, id="row9-end-frame"),
])
async def test_interrupt_remaps_only_the_non_completed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    client: Callable[..., Any], status: str | None, expected: int,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-06-rc")
    if status is not None:
        (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
        State(id=quest.id, step=None, step_attempt=0, status=status).save(
            quest.state_dir)
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", client)

    coord = InterruptCoordinator()
    # The durable flag alone — should_pause stays False, as it is after a gesture
    # round ends, so the remap cannot be satisfied by reading the live latch.
    coord._interrupted = True
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new", stream=_watched())

    assert rc == expected
