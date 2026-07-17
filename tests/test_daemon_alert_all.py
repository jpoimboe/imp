from __future__ import annotations

from imp.remote.daemon.alert_all import AlertAll
from imp.remote.daemon.subscriptions import Subscriptions
from imp.remote.daemon.telegram_adapter import Inbound, TelegramNorthAdapter

TS = "2026-06-25T14:32:05+00:00"


def _on() -> AlertAll:
    a = AlertAll()
    a.set(True)
    return a


def _off() -> AlertAll:
    a = AlertAll()
    a.set(False)
    return a


def _adapter(
    sent: list, *, message_id: int | None = 42, alert_all: AlertAll | None = None,
) -> TelegramNorthAdapter:
    def send(text: str, silent: bool) -> int | None:
        sent.append((text, silent))
        return message_id

    return TelegramNorthAdapter(
        chat_id=4242, submit=lambda _: None,
        send=send, get_updates=lambda _: {"result": []},
        subscriptions=Subscriptions(),  # empty → every Quest takes the alert-all path
        alert_all=alert_all if alert_all is not None else _on(),
        is_known_quest=lambda _cid: True)


def _agent_message(text: str, *, replay: bool = False) -> dict:
    event = {"id": "n1", "type": "agent_message", "timestamp": TS,
             "quest": {"id": "k3f9qa", "title": "t"},
             "payload": {"raw": {"content": [{"type": "text", "text": text}]}}}
    if replay:
        event["_replay"] = True
    return event


def _gate(agent_message: str | None = None, *, replay: bool = False) -> dict:
    payload: dict = {"steer_hint": ""}
    if agent_message is not None:
        payload["last_agent_message"] = agent_message
    event = {"id": "a1", "type": "awaiting_user_input", "timestamp": TS,
             "quest": {"id": "k3f9qa", "title": "t"},
             "payload": payload}
    if replay:
        event["_replay"] = True
    return event


def test_default_is_on() -> None:
    assert AlertAll().enabled() is True


def test_off_then_on_round_trip_restores_enabled() -> None:
    a = AlertAll()
    assert a.enabled() is True
    a.set(False)
    assert a.enabled() is False
    a.set(True)
    assert a.enabled() is True


def test_set_true_enables() -> None:
    a = AlertAll()
    a.set(False)
    a.set(True)
    assert a.enabled() is True


def test_set_false_disables() -> None:
    a = AlertAll()
    a.set(True)
    a.set(False)
    assert a.enabled() is False


def test_seen_is_false_until_marked() -> None:
    assert AlertAll().seen("c1", "e1") is False


def test_mark_then_seen_is_true() -> None:
    a = AlertAll()
    a.mark("c1", "e1")
    assert a.seen("c1", "e1") is True


def test_mark_is_keyed_on_both_change_and_event() -> None:
    a = AlertAll()
    a.mark("c1", "e1")
    assert a.seen("c1", "e2") is False
    assert a.seen("c2", "e1") is False


def test_set_false_clears_the_delivery_log() -> None:
    a = AlertAll()
    a.mark("c1", "e1")
    a.set(False)
    assert a.seen("c1", "e1") is False


def test_alert_all_gate_renders_agent_message_without_label() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_gate("Should I merge the branch?"))
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "Should I merge the branch?" in loud[0]
    assert "needs your input" not in loud[0]
    assert "↩ @k3f9qa" in loud[0]


def test_alert_all_gate_uses_own_payload_not_prior_session_message() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_agent_message("stale earlier prose"))
    adapter.on_event(_gate("the current question"))
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "the current question" in loud[0]
    assert "stale earlier prose" not in loud[0]


def test_alert_all_gate_falls_back_to_label_when_no_agent_message() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_gate(None))
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "needs your input" in loud[0]
    assert "↩ @k3f9qa" in loud[0]


def test_alert_all_gate_falls_back_to_label_when_agent_message_empty() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_gate(""))
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "needs your input" in loud[0]
    assert "↩ @k3f9qa" in loud[0]


def test_alert_all_gate_with_message_is_reply_routable() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_gate("Should I merge the branch?"))
    assert adapter.resolve(
        Inbound(text="yes do it", reply_to_message_id=42)) == ("k3f9qa", "yes do it")
    assert adapter.resolve(
        Inbound(text="@k3f9qa go", reply_to_message_id=None)) == ("k3f9qa", "go")


def test_alert_all_replayed_gate_is_marked_not_sent() -> None:
    sent: list = []
    alert = _on()
    adapter = _adapter(sent, alert_all=alert)
    adapter.on_event(_gate("Should I merge the branch?", replay=True))
    assert sent == []
    assert alert.seen("k3f9qa", "a1") is True


def test_adapter_has_no_last_narration_attribute() -> None:
    adapter = _adapter([])
    assert not hasattr(adapter, "_last_narration")
