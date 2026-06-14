from __future__ import annotations

import queue
import threading

import pytest

from imp.remote.daemon.commands import (
    HELP_TEXT,
    Abandon,
    Create,
    Help,
    ListChanges,
    ListResult,
    Notice,
    Replay,
    Reply,
    Subscribe,
    Unsubscribe,
    ROUTE_HINT,
)
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.focus import Focus
from imp.remote.daemon.runner import _dispatch_inbound, _handle_command


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeControl:
    def __init__(
        self, changes: list[ChangeInfo] | None = None, *, abandon_rc: int = 0,
        snapshots: dict | None = None, narratives: dict | None = None,
    ) -> None:
        self.replies: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.abandoned: list[str] = []
        self._changes = changes or []
        self._abandon_rc = abandon_rc
        self.focus = Focus()
        self._snapshots = snapshots or {}
        self._narratives = narratives or {}

    def snapshot(self, change_id: str) -> tuple[str, str, str] | None:
        return self._snapshots.get(change_id)

    def recent_narrative(self, change_id: str, n: int) -> list[str]:
        return self._narratives.get(change_id, [])[-n:]

    async def reply(self, change_id: str, text: str) -> str:
        self.replies.append((change_id, text))
        return "live"

    def create(self, prompt: str) -> None:
        self.created.append(prompt)

    def abandon(self, change_id: str) -> int:
        self.abandoned.append(change_id)
        return self._abandon_rc

    def list_changes(self) -> list[ChangeInfo]:
        return self._changes


@pytest.mark.anyio
async def test_reply_calls_reply_and_enqueues_nothing() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("2026-06-13-aaaaaa", "go"), control, responses)
    assert control.replies == [("2026-06-13-aaaaaa", "go")]
    assert responses.empty()


@pytest.mark.anyio
async def test_create_calls_create_and_enqueues_a_notice() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Create("add a foo"), control, responses)
    assert control.created == ["add a foo"]
    assert isinstance(responses.get_nowait(), Notice)


@pytest.mark.anyio
async def test_abandon_success_enqueues_abandoned_notice() -> None:
    control = FakeControl(abandon_rc=0)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Abandon("2026-06-13-k3f9qa"), control, responses)
    assert control.abandoned == ["2026-06-13-k3f9qa"]
    notice = responses.get_nowait()
    assert isinstance(notice, Notice) and notice.text == "Abandoned @k3f9qa."
    assert responses.empty()   # no optimistic pre-result ack


@pytest.mark.anyio
async def test_abandon_running_change_enqueues_still_running_notice() -> None:
    control = FakeControl(abandon_rc=2)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Abandon("2026-06-13-k3f9qa"), control, responses)
    assert control.abandoned == ["2026-06-13-k3f9qa"]
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert "k3f9qa" in notice.text and "still running" in notice.text
    assert responses.empty()


@pytest.mark.anyio
async def test_listchanges_calls_list_and_enqueues_a_listresult() -> None:
    rows = [ChangeInfo(id="2026-06-13-aaaaaa", title="T", status="running", step="plan")]
    control = FakeControl(changes=rows)
    responses: queue.Queue = queue.Queue()
    await _handle_command(ListChanges(), control, responses)
    result = responses.get_nowait()
    assert isinstance(result, ListResult) and result.changes == rows


@pytest.mark.anyio
async def test_help_enqueues_the_help_text() -> None:
    responses: queue.Queue = queue.Queue()
    await _handle_command(Help(), FakeControl(), responses)
    assert responses.get_nowait() == Notice(HELP_TEXT)


@pytest.mark.anyio
async def test_notice_passes_through() -> None:
    responses: queue.Queue = queue.Queue()
    await _handle_command(Notice("hint"), FakeControl(), responses)
    assert responses.get_nowait() == Notice("hint")


@pytest.mark.anyio
async def test_reply_auto_focuses_the_change() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("2026-06-14-aaaaaa", "go"), control, responses)
    assert control.focus.current() == "2026-06-14-aaaaaa"
    assert responses.empty()


@pytest.mark.anyio
async def test_subscribe_sets_focus_catches_up_and_confirms() -> None:
    control = FakeControl(
        snapshots={"2026-06-14-k3f9qa": ("running", "implement", "Dark mode")},
        narratives={"2026-06-14-k3f9qa": ["earlier", "latest message"]})
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("2026-06-14-k3f9qa"), control, responses)
    assert control.focus.current() == "2026-06-14-k3f9qa"
    catchup = responses.get_nowait()
    assert isinstance(catchup, Notice)
    assert catchup.text == "@k3f9qa  Dark mode — running · implement\nlatest message"
    confirm = responses.get_nowait()
    assert confirm == Notice("Following @k3f9qa.")
    assert responses.empty()


@pytest.mark.anyio
async def test_subscribe_with_no_narrative_is_header_only() -> None:
    control = FakeControl(
        snapshots={"2026-06-14-k3f9qa": ("running", "plan", "Dark mode")})
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("2026-06-14-k3f9qa"), control, responses)
    catchup = responses.get_nowait()
    assert catchup == Notice("@k3f9qa  Dark mode — running · plan")
    confirm = responses.get_nowait()
    assert confirm == Notice("Following @k3f9qa.")
    assert responses.empty()


@pytest.mark.anyio
async def test_subscribe_with_unknown_change_emits_handle_only_header() -> None:
    control = FakeControl()   # no snapshots — snapshot() returns None
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("2026-06-14-k3f9qa"), control, responses)
    catchup = responses.get_nowait()
    assert catchup == Notice("@k3f9qa")
    confirm = responses.get_nowait()
    assert confirm == Notice("Following @k3f9qa.")
    assert responses.empty()


@pytest.mark.anyio
async def test_unsubscribe_clears_focus_and_confirms() -> None:
    control = FakeControl()
    control.focus.set("2026-06-14-k3f9qa")
    responses: queue.Queue = queue.Queue()
    await _handle_command(Unsubscribe(), control, responses)
    assert control.focus.current() is None
    assert responses.get_nowait() == Notice("Muted — lifecycle alerts still on.")


@pytest.mark.anyio
async def test_replay_resends_each_message_oldest_first() -> None:
    control = FakeControl(
        narratives={"2026-06-14-k3f9qa": ["one", "two", "three"]})
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay("2026-06-14-k3f9qa", 2), control, responses)
    assert responses.get_nowait() == Notice("two")
    assert responses.get_nowait() == Notice("three")
    assert responses.empty()


@pytest.mark.anyio
async def test_replay_falls_back_to_current_focus() -> None:
    control = FakeControl(narratives={"2026-06-14-k3f9qa": ["only"]})
    control.focus.set("2026-06-14-k3f9qa")
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay(None, 5), control, responses)
    assert responses.get_nowait() == Notice("only")


@pytest.mark.anyio
async def test_replay_with_no_target_and_no_focus_is_route_hint() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay(None, 5), control, responses)
    assert responses.get_nowait() == Notice(ROUTE_HINT)


@pytest.mark.anyio
async def test_replay_with_no_narrative_notes_nothing_to_replay() -> None:
    control = FakeControl()
    control.focus.set("2026-06-14-k3f9qa")
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay(None, 5), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice) and "Nothing to replay" in notice.text


class FailThenStopControl:
    """`reply` raises for the `"boom"` change, records others, and stops the loop
    once the second (good) command lands."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.calls: list[str] = []

    async def reply(self, change_id: str, text: str) -> str:
        self.calls.append(change_id)
        if change_id == "boom":
            raise RuntimeError("kaboom")
        self.stop.set()
        return "live"


@pytest.mark.anyio
async def test_dispatch_inbound_survives_a_failing_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A command that makes `_handle_command` raise must not crash the dispatch
    # loop or stall the queue: the loop logs and continues to the next command.
    stop = threading.Event()
    inbound: queue.Queue = queue.Queue()
    inbound.put(Reply("boom", "x"))
    inbound.put(Reply("2026-06-13-good", "y"))
    control = FailThenStopControl(stop)
    responses: queue.Queue = queue.Queue()

    with caplog.at_level("WARNING"):
        await _dispatch_inbound(inbound, control, responses, stop)

    # The loop processed the good command *after* the failing one — proof it
    # neither crashed nor dropped the rest of the queue.
    assert control.calls == ["boom", "2026-06-13-good"]
    assert "command failed" in caplog.text
