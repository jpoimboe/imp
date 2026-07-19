from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from imp.engine.errors import LayoutMismatch, LoadError
from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.commands import (
    ALERTS_OFF_REPLY,
    ALERTS_ON_REPLY,
    HELP_TEXT,
    ROUTE_HINT,
    Alerts,
    Create,
    Help,
    ListQuests,
    ListResult,
    Notice,
    Replay,
    Reply,
    Restart,
    Subscribe,
    Unsubscribe,
)
from imp.remote.daemon.discover import QuestInfo
from imp.remote.daemon.runner import _dispatch_inbound, _handle_command
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.session.reply import DeliveryOutcome


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeControl:
    def __init__(
        self, quests: list[QuestInfo] | None = None, *,
        snapshots: dict | None = None, narratives: dict | None = None,
        resident: frozenset[str] | None = None,
        reply_outcome: DeliveryOutcome = DeliveryOutcome.DELIVERED,
        reply_error: Exception | None = None,
    ) -> None:
        self.replies: list[tuple[str, str]] = []
        self.created: list[str] = []
        self._quests = quests or []
        self.subscriptions = Subscriptions()
        self._snapshots = snapshots or {}
        self._narratives = narratives or {}
        self._histories: dict = {}
        self._resident = frozenset(resident or ())
        self._reply_outcome = reply_outcome
        self._reply_error = reply_error

    def full_history(self, quest_id: str) -> list[dict]:
        return self._histories.get(quest_id, [])

    def snapshot(self, quest_id: str) -> tuple[str, str, str] | None:
        return self._snapshots.get(quest_id)

    def recent_narrative(self, quest_id: str, n: int) -> list[dict]:
        return self._narratives.get(quest_id, [])[-n:]

    async def reply(self, quest_id: str, text: str) -> DeliveryOutcome:
        self.replies.append((quest_id, text))
        if self._reply_error is not None:
            raise self._reply_error
        return self._reply_outcome

    def create(self, prompt: str) -> None:
        self.created.append(prompt)

    def list_quests(self) -> list[QuestInfo]:
        return self._quests

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
async def test_listquests_calls_list_and_enqueues_a_listresult() -> None:
    rows = [QuestInfo(id="aaaaaa", title="T", status="running", step="plan")]
    control = FakeControl(quests=rows, resident=frozenset({"aaaaaa"}))
    responses: queue.Queue = queue.Queue()
    await _handle_command(ListQuests(), control, responses)
    result = responses.get_nowait()
    assert isinstance(result, ListResult)
    assert result.quests == rows
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
async def test_reply_delivered_enqueues_no_notice() -> None:
    control = FakeControl(reply_outcome=DeliveryOutcome.DELIVERED)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("aaaaaa", "go"), control, responses)
    assert control.replies == [("aaaaaa", "go")]
    assert responses.empty()


@pytest.mark.anyio
async def test_reply_not_at_gate_enqueues_dropped_notice() -> None:
    control = FakeControl(reply_outcome=DeliveryOutcome.NOT_AT_GATE)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("aaaaaa", "go"), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert notice.text == "@aaaaaa: not awaiting input — reply dropped."
    assert responses.empty()


@pytest.mark.anyio
async def test_reply_unreachable_enqueues_timeout_notice() -> None:
    control = FakeControl(reply_outcome=DeliveryOutcome.UNREACHABLE)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Reply("aaaaaa", "go"), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert notice.text == "@aaaaaa: delivery timed out — reply dropped."
    assert responses.empty()


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
        {"id": "e1", "type": "agent_result", "timestamp": "2026-06-25T14:32:05+00:00",
         "quest": {"id": "k3f9qa", "title": "Dark mode"}, "payload": {"text": "hi"}},
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
    assert notice.text == "No quest @bogus — send /list to see what's running."
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
        "id": eid, "type": "agent_message",
        "timestamp": "2026-06-25T14:32:05+00:00",
        "quest": {"id": "k3f9qa", "title": "Dark mode"},
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
    # Full envelope: id · title · time header + body. A narrative (agent_message)
    # event carries NO reply footer (only awaiting_user_input gates append the
    # handle); routing still works via the Notice's quest_id tag, asserted below.
    assert isinstance(first, Notice) and isinstance(second, Notice)
    assert first.text.startswith("k3f9qa · Dark mode · ")
    assert first.text.endswith("\none")
    assert "↩" not in first.text
    assert second.text.startswith("k3f9qa · Dark mode · ")
    assert second.text.endswith("\ntwo")
    assert "↩" not in second.text
    # Each replayed Notice is tagged so a reply routes back to the Quest.
    assert first.quest_id == "k3f9qa"
    assert second.quest_id == "k3f9qa"


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

    async def reply(self, quest_id: str, text: str) -> DeliveryOutcome:
        self.calls.append(quest_id)
        if quest_id == "boom":
            raise RuntimeError("kaboom")
        self.stop.set()
        return DeliveryOutcome.DELIVERED


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
        QuestInfo(id="aaaaaa", title="Alpha", status="paused", step="plan"),
        QuestInfo(id="cccccc", title="Gamma", status="merge_blocked", step="finalize"),
    ]
    control = FakeControl(quests=rows)
    responses: queue.Queue = queue.Queue()
    alert_all = AlertAll()
    alert_all.set(False)
    await _handle_command(Alerts(True), control, responses, alert_all)
    assert alert_all.enabled() is True
    assert responses.get_nowait() == Notice(ALERTS_ON_REPLY)
    assert responses.empty()   # no catch-up even with parked Quests on disk


@pytest.mark.anyio
async def test_alerts_on_with_no_parked_changes_sends_only_confirmation() -> None:
    rows = [QuestInfo(id="bbbbbb", title="Beta", status="running", step="implement")]
    control = FakeControl(quests=rows)
    responses: queue.Queue = queue.Queue()
    await _handle_command(Alerts(True), control, responses, AlertAll())
    assert responses.get_nowait() == Notice(ALERTS_ON_REPLY)
    assert responses.empty()   # nothing parked -> no extra catch-up


@pytest.mark.anyio
async def test_reply_layout_mismatch_enqueues_restart_notice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    skew = LayoutMismatch(
        stored=3, engine=2, imp_commit="abc123", quest_dir=Path("/q/aaaaaa"))
    control = FakeControl(reply_error=skew)
    responses: queue.Queue = queue.Queue()
    with caplog.at_level("WARNING"):
        await _handle_command(Reply("aaaaaa", "go"), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert notice.text == (
        "@aaaaaa: this remote-control daemon is running an older imp than the "
        "quest — restart it (imp remote-control stop, then start) to reply.")
    assert responses.empty()
    assert control.replies == [("aaaaaa", "go")]
    # The real cause is logged with a traceback (previously logged nothing).
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.anyio
async def test_reply_load_error_keeps_unknown_notice_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = FakeControl(reply_error=LoadError("quest gone"))
    responses: queue.Queue = queue.Queue()
    with caplog.at_level("WARNING"):
        await _handle_command(Reply("aaaaaa", "go"), control, responses)
    notice = responses.get_nowait()
    assert isinstance(notice, Notice)
    assert notice.text == "no such quest `aaaaaa` — unknown or pruned"
    assert responses.empty()
    assert control.replies == [("aaaaaa", "go")]
    # The swallowed cause is now logged with a traceback.
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.anyio
async def test_restart_invokes_request_restart_and_enqueues_nothing() -> None:
    calls: list[str] = []
    responses: queue.Queue = queue.Queue()
    await _handle_command(
        Restart(), FakeControl(), responses,
        request_restart=lambda: calls.append("restart"))
    assert calls == ["restart"]
    assert responses.empty()


@pytest.mark.anyio
async def test_dispatch_inbound_threads_request_restart_to_handler() -> None:
    stop = threading.Event()
    inbound: queue.Queue = queue.Queue()
    inbound.put(Restart())
    control = FakeControl()
    responses: queue.Queue = queue.Queue()
    calls: list[str] = []

    def request_restart() -> None:
        calls.append("restart")
        stop.set()

    await _dispatch_inbound(
        inbound, control, responses, stop, AlertAll(), request_restart)

    assert calls == ["restart"]
    assert responses.empty()
