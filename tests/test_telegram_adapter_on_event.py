from __future__ import annotations

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import Inbound, TelegramNorthAdapter

CHANGE_ID = "aabbcc"
OTHER_CHANGE_ID = "zzxxww"
TS = "2026-06-25T14:32:05+00:00"

LOUD_EVENT = {
    "id": "L1", "type": "awaiting_user_input", "timestamp": TS,
    "payload": {"prompt": "Need your call"},
    "change": {"id": "k3f9qa", "title": "Dark mode"},
}

SILENT_EVENT = {
    "id": "S1", "type": "agent_output", "timestamp": TS,
    "payload": {"text": "working on it"},
    "change": {"id": CHANGE_ID, "title": "My change"},
}


def _subs(*change_ids: str) -> Subscriptions:
    subs = Subscriptions()
    for cid in change_ids:
        subs.add(cid)
    return subs


def _adapter(
    sent: list, *, message_id: int | None = 42,
    subscriptions: Subscriptions | None = None,
    alert_all: AlertAll | None = None,
) -> TelegramNorthAdapter:
    def send(text: str, silent: bool) -> int | None:
        sent.append((text, silent))
        return message_id

    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda _: None,
        send=send, get_updates=lambda _: {"result": []},
        subscriptions=subscriptions if subscriptions is not None else _subs("k3f9qa"),
        alert_all=alert_all)


def test_on_event_sends_a_message_carrying_the_handle() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(LOUD_EVENT)
    assert len(sent) == 1
    text, silent = sent[0]
    assert text.startswith("k3f9qa · Dark mode · ")
    assert "k3f9qa" in text and "Need your call" in text
    assert silent is False


def test_on_event_records_reply_to_mapping_so_a_reply_resolves_home() -> None:
    adapter = _adapter([])
    adapter.on_event(LOUD_EVENT)
    reply = Inbound(text="yes do it", reply_to_message_id=42)
    assert adapter.resolve(reply) == ("k3f9qa", "yes do it")


def test_on_event_records_handle_mapping_for_the_at_fallback() -> None:
    adapter = _adapter([])
    adapter.on_event(LOUD_EVENT)
    reply = Inbound(text="@k3f9qa go", reply_to_message_id=None)
    assert adapter.resolve(reply) == ("k3f9qa", "go")


def test_on_event_drops_a_silent_event_with_an_empty_body() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event({"id": "x", "type": "llm_response", "timestamp": TS,
                      "payload": {}, "change": {"id": "k3f9qa", "title": "X"}})
    assert sent == []


def test_on_event_records_handle_map_even_when_send_returns_none() -> None:
    adapter = _adapter([], message_id=None)
    adapter.on_event(LOUD_EVENT)
    reply = Inbound(text="@k3f9qa go", reply_to_message_id=None)
    assert adapter.resolve(reply) == ("k3f9qa", "go")


def test_unsubscribed_event_is_dropped_and_records_no_maps() -> None:
    adapter = _adapter([], subscriptions=Subscriptions())
    adapter.on_event(SILENT_EVENT)
    assert adapter._handle_to_change == {}
    assert adapter._msg_to_change == {}


def _narration(text: str) -> dict:
    return {"id": "n1", "type": "llm_response", "timestamp": TS,
            "change": {"id": "k3f9qa", "title": "t"},
            "payload": {"raw": {"content": [{"type": "text", "text": text}]}}}


def _prompt_less_await() -> dict:
    return {"id": "a1", "type": "awaiting_user_input", "timestamp": TS,
            "change": {"id": "k3f9qa", "title": "t"},
            "payload": {"prompt": ""}}


def test_prompt_less_await_falls_back_to_preceding_narration() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Should I merge the branch?"))
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert any("Should I merge the branch?" in text for text in loud)


def test_prompt_less_await_with_no_narration_sends_header_only() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1


def test_consecutive_prompt_less_awaits_reuse_last_narration() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Proceed with the merge?"))
    adapter.on_event(_prompt_less_await())
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 2
    assert all("Proceed with the merge?" in text for text in loud)


def _step_started(
    change_id: str, *, phase: str | None = None, label: str = "scoping",
    quiet: bool = False,
) -> dict:
    payload: dict = {"label": label}
    if phase is not None:
        payload["phase_label"] = phase
    if quiet:
        payload["quiet"] = True
    return {"id": "st1", "type": "step_started", "timestamp": TS,
            "change": {"id": change_id, "title": "t"}, "payload": payload}


def test_step_started_renders_silent_header_less_divider_when_subscribed() -> None:
    sent: list = []
    _adapter(sent, subscriptions=_subs("k3f9qa")).on_event(
        _step_started("k3f9qa", phase="Brainstorm", label="scoping"))
    assert sent == [("— Brainstorm › scoping —", True)]


def test_step_started_divider_dropped_when_unsubscribed() -> None:
    sent: list = []
    _adapter(sent, subscriptions=Subscriptions()).on_event(_step_started("k3f9qa"))
    assert sent == []


def test_quiet_step_started_renders_no_divider() -> None:
    sent: list = []
    _adapter(sent, subscriptions=_subs("k3f9qa")).on_event(
        _step_started("k3f9qa", quiet=True))
    assert sent == []


def test_prompt_less_await_does_not_borrow_another_changes_narration() -> None:
    sent: list = []
    adapter = _adapter(sent, subscriptions=_subs("k3f9qa", "other"))
    adapter.on_event(_narration("A's secret context"))   # narration for k3f9qa
    adapter.on_event({"id": "a2", "type": "awaiting_user_input", "timestamp": TS,
                      "change": {"id": "other", "title": "t"}, "payload": {"prompt": ""}})
    loud = [text for text, silent in sent if not silent]
    assert loud and all("A's secret context" not in text for text in loud)


SILENT_K3 = {
    "id": "S2", "type": "agent_output", "timestamp": TS,
    "payload": {"text": "working on it"},
    "change": {"id": "k3f9qa", "title": "Dark mode"},
}


def _alert_all_on() -> AlertAll:
    a = AlertAll()
    a.set(True)
    return a


def test_alert_all_off_drops_unsubscribed_buzz_and_records_no_maps() -> None:
    sent: list = []
    adapter = _adapter(sent, subscriptions=Subscriptions())  # alert-all default off
    adapter.on_event(LOUD_EVENT)
    assert sent == []
    assert adapter._handle_to_change == {}
    assert adapter._msg_to_change == {}


def test_alert_all_on_sends_unsubscribed_buzz_loud_and_records_maps() -> None:
    sent: list = []
    adapter = _adapter(
        sent, subscriptions=Subscriptions(), alert_all=_alert_all_on())
    adapter.on_event(LOUD_EVENT)
    assert len(sent) == 1
    text, silent = sent[0]
    assert silent is False
    assert "Need your call" in text
    assert adapter._handle_to_change == {"k3f9qa": "k3f9qa"}
    assert adapter._msg_to_change == {42: "k3f9qa"}


def test_alert_all_on_still_drops_unsubscribed_silent_narrative() -> None:
    sent: list = []
    adapter = _adapter(
        sent, subscriptions=Subscriptions(), alert_all=_alert_all_on())
    adapter.on_event(SILENT_K3)   # agent_output -> silent tier
    assert sent == []


def test_alert_all_dedups_replayed_buzz_by_event_id() -> None:
    sent: list = []
    adapter = _adapter(
        sent, subscriptions=Subscriptions(), alert_all=_alert_all_on())
    adapter.on_event(LOUD_EVENT)
    adapter.on_event(LOUD_EVENT)   # same event id "L1"
    assert len(sent) == 1


def test_alert_all_is_best_effort_when_event_id_missing() -> None:
    sent: list = []
    adapter = _adapter(
        sent, subscriptions=Subscriptions(), alert_all=_alert_all_on())
    no_id = {k: v for k, v in LOUD_EVENT.items() if k != "id"}
    adapter.on_event(no_id)
    adapter.on_event(no_id)
    assert len(sent) == 2


def test_alert_all_failed_send_does_not_mark_so_replay_re_alerts() -> None:
    sent: list = []
    # message_id=None models a send that fails without raising.
    adapter = _adapter(
        sent, message_id=None,
        subscriptions=Subscriptions(), alert_all=_alert_all_on())
    adapter.on_event(LOUD_EVENT)
    adapter.on_event(LOUD_EVENT)   # same event id "L1"
    assert len(sent) == 2   # not deduped: the failed send was never marked seen


def test_subscribed_change_unaffected_by_alert_all_on() -> None:
    sent: list = []
    adapter = _adapter(
        sent, subscriptions=_subs("k3f9qa"), alert_all=_alert_all_on())
    adapter.on_event(LOUD_EVENT)
    adapter.on_event(SILENT_K3)
    assert len(sent) == 2
    assert any(not silent for _, silent in sent)   # buzz delivered loud
    assert any(silent for _, silent in sent)        # narrative delivered silent
