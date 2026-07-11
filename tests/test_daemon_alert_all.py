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
        alert_all=alert_all if alert_all is not None else _on())


def _narration(text: str, *, replay: bool = False) -> dict:
    event = {"id": "n1", "type": "llm_response", "timestamp": TS,
             "change": {"id": "k3f9qa", "title": "t"},
             "payload": {"raw": {"content": [{"type": "text", "text": text}]}}}
    if replay:
        event["_replay"] = True
    return event


def _prompt_less_await() -> dict:
    return {"id": "a1", "type": "awaiting_user_input", "timestamp": TS,
            "change": {"id": "k3f9qa", "title": "t"},
            "payload": {"prompt": ""}}


def _prompt_await(prompt: str) -> dict:
    return {"id": "a2", "type": "awaiting_user_input", "timestamp": TS,
            "change": {"id": "k3f9qa", "title": "t"},
            "payload": {"prompt": prompt}}


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


def test_alert_all_prompt_less_await_carries_last_narration() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Should I merge the branch?"))
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "Should I merge the branch?" in loud[0]
    assert "needs your input" in loud[0]


def test_alert_all_prompt_less_await_alert_is_reply_routable() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Should I merge the branch?"))
    adapter.on_event(_prompt_less_await())
    assert adapter.resolve(
        Inbound(text="yes do it", reply_to_message_id=42)) == ("k3f9qa", "yes do it")
    assert adapter.resolve(
        Inbound(text="@k3f9qa go", reply_to_message_id=None)) == ("k3f9qa", "go")


def test_alert_all_non_empty_prompt_renders_prompt_not_narration() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Should I merge the branch?"))
    adapter.on_event(_prompt_await("Pick option A or B"))
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "Pick option A or B" in loud[0]
    assert "Should I merge the branch?" not in loud[0]


def test_alert_all_prompt_less_await_with_no_narration_sends_header_only() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "needs your input" in loud[0]


def test_alert_all_narration_tracked_while_off_is_used_after_on() -> None:
    sent: list = []
    alert = _off()
    adapter = _adapter(sent, alert_all=alert)
    adapter.on_event(_narration("Proceed with the merge?"))  # tracked while OFF
    alert.set(True)
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "Proceed with the merge?" in loud[0]


def test_alert_all_replayed_narration_does_not_track() -> None:
    sent: list = []
    adapter = _adapter(sent)
    adapter.on_event(_narration("Pre-boot context", replay=True))  # history: not tracked
    adapter.on_event(_prompt_less_await())
    loud = [text for text, silent in sent if not silent]
    assert len(loud) == 1
    assert "Pre-boot context" not in loud[0]
    assert adapter._last_narration == {}
