from __future__ import annotations

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import TelegramNorthAdapter

A = "2026-06-14-aaaaaa"
B = "2026-06-14-bbbbbb"
TS = "2026-06-25T14:32:05+00:00"


def _adapter(subs: Subscriptions, sent: list) -> TelegramNorthAdapter:
    alert_all = AlertAll()
    alert_all.set(False)  # isolate the focus gate — default is now on
    return TelegramNorthAdapter(
        chat_id=1, submit=lambda command: None,
        send=lambda text, silent: (sent.append((text, silent)) or 1),
        get_updates=lambda offset: {"result": []}, subscriptions=subs,
        alert_all=alert_all)


def _silent(change_id: str, event_id: str = "s1") -> dict:
    return {"id": event_id, "type": "agent_output", "timestamp": TS,
            "payload": {"text": "narrative"}, "change": {"id": change_id, "title": "t"}}


def _buzz(change_id: str, event_id: str = "b1") -> dict:
    return {"id": event_id, "type": "awaiting_user_input", "timestamp": TS,
            "payload": {"prompt": "?"}, "change": {"id": change_id, "title": "t"}}


def test_unsubscribed_change_sends_nothing_silent() -> None:
    subs = Subscriptions()
    sent: list = []
    _adapter(subs, sent).on_event(_silent(A))
    assert sent == []


def test_unsubscribed_change_sends_nothing_buzz() -> None:
    subs = Subscriptions()
    sent: list = []
    _adapter(subs, sent).on_event(_buzz(A))
    assert sent == []


def test_subscribed_silent_event_streams_quietly() -> None:
    subs = Subscriptions()
    subs.add(A)
    sent: list = []
    _adapter(subs, sent).on_event(_silent(A))
    assert len(sent) == 1
    assert sent[0][1] is True


def test_subscribed_buzz_event_buzzes() -> None:
    subs = Subscriptions()
    subs.add(A)
    sent: list = []
    _adapter(subs, sent).on_event(_buzz(A))
    assert len(sent) == 1
    assert sent[0][1] is False


def test_only_the_subscribed_change_streams() -> None:
    subs = Subscriptions()
    subs.add(A)
    sent: list = []
    _adapter(subs, sent).on_event(_silent(B))
    assert sent == []


def test_dropped_type_stays_dropped_when_subscribed() -> None:
    subs = Subscriptions()
    subs.add(A)
    sent: list = []
    _adapter(subs, sent).on_event(
        {"id": "t1", "type": "thinking", "timestamp": TS, "payload": {},
         "change": {"id": A, "title": "t"}})
    assert sent == []


def test_subscribed_silent_empty_body_stays_dropped() -> None:
    subs = Subscriptions()
    subs.add(A)
    sent: list = []
    _adapter(subs, sent).on_event(
        {"id": "e0", "type": "agent_output", "timestamp": TS,
         "payload": {"text": ""}, "change": {"id": A, "title": "t"}})
    assert sent == []
