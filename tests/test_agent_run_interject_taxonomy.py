from __future__ import annotations

from imp.engine.agent_run import deliver_interject, drain_all_interjects_at_resume
from imp.engine.state import InMemoryEventLog


class _Coord:
    def __init__(self, texts):
        self._texts = list(texts)

    def drain_interject(self):
        return self._texts.pop(0) if self._texts else None


def test_deliver_interject_emits_single_user_message_interject() -> None:
    log = InMemoryEventLog()
    delivered = deliver_interject(_Coord(["stop and reconsider"]), [], [], log, "step:a")
    assert delivered is True
    evs = list(log.read())
    assert [e["type"] for e in evs] == ["user_message"]
    assert evs[0]["payload"]["subtype"] == "interject"
    assert evs[0]["payload"]["content"][0]["text"] == "stop and reconsider"


def test_drain_all_interjects_at_resume_emits_one_event_per_interject() -> None:
    log = InMemoryEventLog()
    messages: list = []
    transcript: list = []
    count = drain_all_interjects_at_resume(
        _Coord(["first", "second"]), messages, transcript, log, "step:b"
    )
    assert count == 2
    evs = list(log.read())
    assert [e["type"] for e in evs] == ["user_message", "user_message"]
    assert evs[0]["payload"]["subtype"] == "interject"
    assert evs[1]["payload"]["subtype"] == "interject"
    assert evs[0]["payload"]["content"][0]["text"] == "first"
    assert evs[1]["payload"]["content"][0]["text"] == "second"


def test_drain_all_interjects_at_resume_empty_queue_returns_zero() -> None:
    log = InMemoryEventLog()
    count = drain_all_interjects_at_resume(_Coord([]), [], [], log, "step:c")
    assert count == 0
    assert list(log.read()) == []
