"""Every closing-block row exits with its contract code, whatever an earlier
interrupt did."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.cli.palette import sgr
from imp.cli.session_run import run_session_client
from imp.cli.signals import InterruptCoordinator
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.client import StreamEnding
from tests.test_session_run import (
    _abandoning,
    _cut,
    _ended,
    _ended_by,
    _ending_with,
    _leaving,
    _make_quest,
    _never,
    _NullDriver,
    _watched,
)

_GOLD_FAILURES = (
    "max_loops", "script_blocked", "sandbox_spawn_failed", "unknown_step", "clean_tree")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _paused(**payload: str) -> Any:
    return _ending_with(
        StreamEnding.ENDED, {"type": "workflow_paused", "payload": payload})


@pytest.mark.anyio
@pytest.mark.parametrize("interrupted", [False, True], ids=["fresh", "after-interrupt"])
@pytest.mark.parametrize(("client", "status", "expected"), [
    pytest.param(_abandoning, "paused", 130, id="abandoned"),
    pytest.param(_leaving(None, intent="save", disposition="save"), "paused", 130,
                 id="saved-as-draft"),
    pytest.param(_leaving(None, intent="shelve", disposition="archive"), "paused", 130,
                 id="shelved"),
    pytest.param(_leaving("ctrl-d", disposition="discard"), "paused", 130,
                 id="discarded"),
    pytest.param(_leaving("ctrl-d", send_failed="1"), "paused", 130, id="left-running"),
    pytest.param(_ended_by("save"), "paused", 130, id="end-frame-leave"),
    pytest.param(
        _ending_with(StreamEnding.CUT,
                     {"type": "quest_merged", "payload": {"base": "main"}}),
        "paused", 0, id="merged"),
    pytest.param(
        _ending_with(StreamEnding.CUT, {"type": "workflow_completed", "payload": {}}),
        "paused", 0, id="done"),
    pytest.param(
        _ending_with(StreamEnding.CUT,
                     {"type": "merge_blocked", "payload": {"reason": "conflict"}}),
        "paused", 1, id="merge-blocked"),
    pytest.param(_leaving("ctrl-d"), "paused", 130, id="ctrl-d"),
    pytest.param(_leaving("sigterm"), "paused", 130, id="sigterm"),
    pytest.param(_leaving("sighup"), "paused", 130, id="sighup"),
    pytest.param(_leaving("sigquit"), "paused", 130, id="sigquit"),
    pytest.param(_leaving(None, intent="save"), "paused", 130, id="save-on-built-quest"),
    pytest.param(_leaving(None), "paused", 130, id="headless-detach"),
    pytest.param(_paused(reason="awaiting_user"), "paused", 130, id="awaiting-user"),
    pytest.param(_paused(reason="awaiting_turn"), "paused", 130, id="awaiting-turn"),
    pytest.param(_paused(reason="interrupt", interrupt_reason="ctrl-c"), "paused", 130,
                 id="headless-ctrl-c"),
    pytest.param(_paused(reason="api", interrupt_reason="sigterm"), "paused", 130,
                 id="interrupt-over-error"),
    pytest.param(_paused(reason="api"), "paused", 1, id="api-error"),
    pytest.param(_paused(reason="terminated"), "paused", 1, id="agent-error"),
    *(pytest.param(_paused(reason=r), "paused", 1, id=r) for r in _GOLD_FAILURES),
    pytest.param(_ended, "paused", 1, id="daemon-died"),
    pytest.param(_cut, "merged", 0, id="disk-completed"),
    pytest.param(_cut, None, 4, id="lost-connection"),
])
async def test_each_ending_exits_with_its_contract_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    client: Callable[..., Any], status: str | None, expected: int, interrupted: bool,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-11-02-rc")
    if status is not None:
        (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
        State(id=quest.id, step=None, step_attempt=0, status=status).save(
            quest.state_dir)
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", client)

    coord = InterruptCoordinator()
    coord._interrupted = interrupted
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new", stream=_watched())

    assert rc == expected


@pytest.mark.anyio
@pytest.mark.parametrize("reason", _GOLD_FAILURES)
async def test_a_gold_failure_pause_exits_1_and_keeps_its_gold_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], reason: str,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("imp.cli.main.should_use_color", lambda _stream: True)
    quest = _make_quest(tmp_path, "2026-11-02-gold")
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _paused(reason=reason))

    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), leave_mode="new", stream=_watched())

    head = capsys.readouterr().out.split("\n")[0]
    assert rc == 1
    assert head.startswith(sgr("wordmark", enabled=True))   # gold, as before
    assert not head.startswith(sgr("red", enabled=True))
