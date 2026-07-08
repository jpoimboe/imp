from __future__ import annotations

import queue
import threading

import pytest

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.commands import (
    ALERTS_OFF_REPLY,
    ALERTS_ON_REPLY,
    HELP_TEXT,
    ROUTE_HINT,
    Abandon,
    Alerts,
    Create,
    Help,
    ListChanges,
    ListResult,
    Notice,
    Replay,
    Reply,
    Subscribe,
    Unsubscribe,
)
from imp.remote.daemon.discover import ChangeInfo
from imp.remote.daemon.runner import _dispatch_inbound, _handle_command
from imp.remote.daemon.subscriptions import Subscriptions


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeControl:
    def __init__(
        self, changes: list[ChangeInfo] | None = None, *, abandon_rc: int = 0,
        snapshots: dict | None = None, narratives: dict | None = None,
        resident: frozenset[str] | None = None,
    ) -> None:
        self.replies: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.abandoned: list[str] = []
        self._changes = changes or []
        self._abandon_rc = abandon_rc
        self.subscriptions = Subscriptions()
        self._snapshots = snapshots or {}
        self._narratives = narratives or {}
        self._histories: dict = {}
        self._resident = frozenset(resident or ())

    def full_history(self, change_id: str) -> list[dict]:
        return self._histories.get(change_id, [])

    def snapshot(self, change_id: str) -> tuple[str, str, str] | None:
        return self._snapshots.get(change_id)

    def recent_narrative(self, change_id: str, n: int) -> list[dict]:
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

    def resident_ids(self) -> frozenset[str]:
        return self._resident


@pytest.mark.anyio
async def test_reply_calls_reply_and_enqueues_nothing() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("aaaaaa", "go"), control, responses)
    assert control.replies == [("aaaaaa", "go")]
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
    await _handle_command(Abandon("k3f9qa"), control, responses)
    assert control.abandoned == ["k3f9qa"]
    notice = responses.get_nowait()
    assert isinstance(notice, Notice) and notice.text == "Abandoned @k3f9qa."
    assert responses.empty()   # no optimistic pre-result ack


@pytest.mark.anyio
async def test_abandon_running_change_enqueues_still_running_notice() -> None:
    control = FakeControl(abandon_rc=2)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Abandon("k3f9qa"), control, responses)
    assert control.abandoned == ["k3f9qa"]
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert "k3f9qa" in notice.text and "still running" in notice.text
    assert responses.empty()


@pytest.mark.anyio
async def test_listchanges_calls_list_and_enqueues_a_listresult() -> None:
    rows = [ChangeInfo(id="aaaaaa", title="T", status="running", step="plan")]
    control = FakeControl(changes=rows, resident=frozenset({"aaaaaa"}))
    responses: queue.Queue = queue.Queue()
    await _handle_command(ListChanges(), control, responses)
    result = responses.get_nowait()
    assert isinstance(result, ListResult)
    assert result.changes == rows
    assert result.resident == frozenset({"aaaaaa"})


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
async def test_reply_does_not_subscribe() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("aaaaaa", "go"), control, responses)
    assert control.subscriptions.contains("aaaaaa") is False
    assert responses.empty()


@pytest.mark.anyio
async def test_subscribe_seeds_seen_set_without_replaying() -> None:
    control = FakeControl(snapshots={"k3f9qa": ("running", "plan", "Dark mode")})
    control._histories["k3f9qa"] = [
        {"id": "e1", "type": "agent_output", "timestamp": "2026-06-25T14:32:05+00:00",
         "change": {"id": "k3f9qa", "title": "Dark mode"}, "payload": {"text": "hi"}},
    ]
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("k3f9qa"), control, responses)
    # History is not replayed — only the confirmation sends.
    assert responses.get_nowait() == Notice("Following @k3f9qa.")
    assert responses.empty()
    assert control.subscriptions.contains("k3f9qa")
    # The snapshot still seeded the seen-set from full_history.
    assert control.subscriptions.seen("k3f9qa", "e1")


@pytest.mark.anyio
async def test_subscribe_with_empty_history_just_confirms() -> None:
    control = FakeControl(snapshots={"k3f9qa": ("running", "plan", "Dark mode")})
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("k3f9qa"), control, responses)
    assert responses.get_nowait() == Notice("Following @k3f9qa.")
    assert responses.empty()
    assert control.subscriptions.contains("k3f9qa")


@pytest.mark.anyio
async def test_subscribe_to_unknown_handle_errors_without_subscribing() -> None:
    control = FakeControl()  # no snapshot wired -> snapshot("bogus") is None
    responses: queue.Queue = queue.Queue()
    await _handle_command(Subscribe("bogus"), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert "bogus" in notice.text
    assert "Following" not in notice.text
    assert responses.empty()
    assert control.subscriptions.contains("bogus") is False


@pytest.mark.anyio
async def test_unsubscribe_removes_the_target_and_confirms() -> None:
    control = FakeControl()
    control.subscriptions.add("k3f9qa", ["e1"])
    responses: queue.Queue = queue.Queue()
    await _handle_command(Unsubscribe("k3f9qa"), control, responses)
    assert control.subscriptions.contains("k3f9qa") is False
    assert responses.get_nowait() == Notice("Unfollowed @k3f9qa.")


def _narr_event(eid: str, text: str) -> dict:
    return {
        "id": eid, "type": "llm_response",
        "timestamp": "2026-06-25T14:32:05+00:00",
        "change": {"id": "k3f9qa", "title": "Dark mode"},
        "payload": {"raw": {"content": [{"type": "text", "text": text}]}},
    }


@pytest.mark.anyio
async def test_replay_renders_each_event_through_the_common_envelope() -> None:
    control = FakeControl(narratives={"k3f9qa": [
        _narr_event("e1", "one"), _narr_event("e2", "two")]})
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay("k3f9qa", 5), control, responses)
    first = responses.get_nowait()
    second = responses.get_nowait()
    assert responses.empty()
    # Full envelope (id · title · time / body / reply footer), not a bare body.
    assert isinstance(first, Notice) and isinstance(second, Notice)
    assert first.text.startswith("k3f9qa · Dark mode · ")
    assert first.text.endswith("\none\n↩ @k3f9qa")
    assert second.text.startswith("k3f9qa · Dark mode · ")
    assert second.text.endswith("\ntwo\n↩ @k3f9qa")
    # Each replayed Notice is tagged so a reply routes back to the Change.
    assert first.change_id == "k3f9qa"
    assert second.change_id == "k3f9qa"


@pytest.mark.anyio
async def test_replay_with_no_target_is_route_hint() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay(None, 5), control, responses)
    assert responses.get_nowait() == Notice(ROUTE_HINT)


@pytest.mark.anyio
async def test_replay_with_no_narrative_notes_nothing_to_replay() -> None:
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    await _handle_command(Replay("k3f9qa", 5), control, responses)
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
    inbound.put(Reply("goodno", "y"))
    control = FailThenStopControl(stop)
    responses: queue.Queue = queue.Queue()

    with caplog.at_level("WARNING"):
        await _dispatch_inbound(inbound, control, responses, stop, AlertAll())

    # The loop processed the good command *after* the failing one — proof it
    # neither crashed nor dropped the rest of the queue.
    assert control.calls == ["boom", "goodno"]
    assert "command failed" in caplog.text


@pytest.mark.anyio
async def test_alerts_on_sets_mode_and_confirms() -> None:
    alert_all = AlertAll()
    alert_all.set(False)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Alerts(True), FakeControl(), responses, alert_all)
    assert alert_all.enabled() is True
    assert responses.get_nowait() == Notice(ALERTS_ON_REPLY)
    assert responses.empty()


@pytest.mark.anyio
async def test_alerts_off_clears_mode_and_confirms() -> None:
    alert_all = AlertAll()
    alert_all.set(True)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Alerts(False), FakeControl(), responses, alert_all)
    assert alert_all.enabled() is False
    assert responses.get_nowait() == Notice(ALERTS_OFF_REPLY)
    assert responses.empty()


@pytest.mark.anyio
async def test_alerts_bare_reports_off_state() -> None:
    alert_all = AlertAll()
    alert_all.set(False)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Alerts(None), FakeControl(), responses, alert_all)
    assert alert_all.enabled() is False   # bare query must not change the mode
    assert responses.get_nowait() == Notice(ALERTS_OFF_REPLY)
    assert responses.empty()


@pytest.mark.anyio
async def test_alerts_bare_reports_on_state() -> None:
    alert_all = AlertAll()
    alert_all.set(True)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Alerts(None), FakeControl(), responses, alert_all)
    assert alert_all.enabled() is True
    assert responses.get_nowait() == Notice(ALERTS_ON_REPLY)
    assert responses.empty()


@pytest.mark.anyio
async def test_alerts_on_with_parked_changes_sends_only_confirmation() -> None:
    rows = [
        ChangeInfo(id="aaaaaa", title="Alpha", status="paused", step="plan"),
        ChangeInfo(id="cccccc", title="Gamma", status="merge_blocked", step="finalize"),
    ]
    control = FakeControl(changes=rows)
    responses: queue.Queue = queue.Queue()
    alert_all = AlertAll()
    alert_all.set(False)
    await _handle_command(Alerts(True), control, responses, alert_all)
    assert alert_all.enabled() is True
    assert responses.get_nowait() == Notice(ALERTS_ON_REPLY)
    assert responses.empty()   # no catch-up even with parked Changes on disk


@pytest.mark.anyio
async def test_alerts_on_with_no_parked_changes_sends_only_confirmation() -> None:
    rows = [ChangeInfo(id="bbbbbb", title="Beta", status="running", step="implement")]
    control = FakeControl(changes=rows)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Alerts(True), control, responses, AlertAll())
    assert responses.get_nowait() == Notice(ALERTS_ON_REPLY)
    assert responses.empty()   # nothing parked -> no extra catch-up
