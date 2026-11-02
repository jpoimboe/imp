from __future__ import annotations

from imp.agents.runner import Message, TextBlock, ToolUseBlock
from imp.agents.transcript import (
    SkillReply,
    Transcript,
    answer_complete_step_on_resume,
    balance_success_on_resume,
    deliver_continuation_reply,
    deliver_engine_continuation,
    deliver_reply_on_resume,
)
from imp.engine.state import InMemoryEventLog


def _types(log: InMemoryEventLog) -> list[str]:
    return [e["type"] for e in log.read()]


def test_reply_on_resume_splits_human_and_engine(tmp_path) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    deliver_reply_on_resume([], tr, log, "step:a", "hello", human=True)
    deliver_reply_on_resume([], tr, log, "step:a", "resuming", human=False)
    evs = list(log.read())
    assert evs[0]["type"] == "user_message"
    assert evs[0]["payload"]["subtype"] == "reply"
    assert evs[1]["type"] == "engine_message"
    assert evs[1]["payload"]["subtype"] == "continuation"


def test_engine_continuation_is_engine_message(tmp_path) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    deliver_engine_continuation([], tr, log, "step:a", "keep going")
    ev = next(iter(log.read()))
    assert ev["type"] == "engine_message"
    assert ev["payload"]["subtype"] == "continuation"
    assert ev["payload"]["content"][0]["text"] == "keep going"
    assert "engine_continuation" not in _types(log)


def test_balance_success_emits_engine_message_continuation(tmp_path) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant", content=(
            ToolUseBlock(id="c1", name="complete_step", input={"status": "Success"}),
        )),
    ]
    for m in msgs:
        tr.append(m)
    balance_success_on_resume(msgs, tr, log, "step:a")
    evs = list(log.read())
    assert _types(log) == ["engine_message"]
    assert evs[0]["payload"]["subtype"] == "continuation"
    assert "user_message" not in _types(log)


def test_answer_complete_step_emits_engine_message_continuation(tmp_path) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    cs = ToolUseBlock(id="c1", name="complete_step", input={"status": "Success"})
    msgs = [
        Message(role="user", content=(TextBlock(text="begin"),)),
        Message(role="assistant", content=(cs,)),
    ]
    for m in msgs:
        tr.append(m)
    answer_complete_step_on_resume(msgs, tr, log, "step:a", cs, None)
    evs = list(log.read())
    assert _types(log) == ["engine_message", "engine_message"]
    assert all(e["payload"]["subtype"] == "continuation" for e in evs)
    assert "user_message" not in _types(log)


def test_deliver_continuation_reply_skill_reply_event_sequence(tmp_path) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    reply = SkillReply(command="/load skill", name="skill", body="skill body text")
    result = deliver_continuation_reply([], tr, log, "step:a", reply, human=True)
    assert result == reply.body
    evs = list(log.read())
    assert [e["type"] for e in evs] == ["user_message", "skill_load", "engine_message"]
    assert evs[0]["payload"]["subtype"] == "reply"
    assert evs[2]["payload"]["subtype"] == "continuation"
    assert evs[2]["payload"]["content"][0]["text"] == reply.body


def test_deliver_continuation_reply_plain_human_emits_user_message_reply(tmp_path) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    result = deliver_continuation_reply([], tr, log, "step:a", "hello", human=True)
    assert result == "hello"
    evs = list(log.read())
    assert _types(log) == ["user_message"]
    assert evs[0]["payload"]["subtype"] == "reply"


def test_deliver_continuation_reply_plain_engine_emits_engine_message_continuation(
    tmp_path,
) -> None:
    log = InMemoryEventLog()
    tr = Transcript(tmp_path / "t.jsonl")
    result = deliver_continuation_reply([], tr, log, "step:a", "keep going", human=False)
    assert result == "keep going"
    evs = list(log.read())
    assert _types(log) == ["engine_message"]
    assert evs[0]["payload"]["subtype"] == "continuation"
