from __future__ import annotations

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript, repair_dangling_tool_use_on_resume
from imp.engine.replay import (
    build_messages_from_events,
    parse_message,
    reconstruct_request_for_response,
)
from imp.engine.state import InMemoryEventLog


def _agent_stream(log: InMemoryEventLog, agent_id: str) -> dict:
    """Emit a step_context, initial user_message, one llm_response with a
    tool_use, a tool_result, then a final llm_response. Return key event ids."""
    log.append_step_context(
        agent_id=agent_id, parent_id_override="seed",
        provider="anthropic", model="m", system_prompt="SYS",
        tools_schema=[{"name": "read_file"}], params={"max_tokens": 8192},
        max_tool_calls=50)
    log.append_user_message(
        agent_id=agent_id,
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    resp1 = log.append_llm_response(
        agent_id=agent_id, provider="anthropic", model="m",
        raw={"content": [{"type": "tool_use", "id": "t1",
                          "name": "read_file", "input": {"p": "x"}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    log.append_tool_call(agent_id=agent_id,
                         block=ToolUseBlock(id="t1", name="read_file", input={"p": "x"}))
    log.append_tool_result(agent_id=agent_id,
                           block=ToolResultBlock(tool_use_id="t1", content="FILE"))
    resp2 = log.append_llm_response(
        agent_id=agent_id, provider="anthropic", model="m",
        raw={"content": [{"type": "text", "text": "done"}],
             "stop_reason": "end_turn", "usage": {}},
        usage={}, stop_reason="end_turn")
    return {"resp1": resp1["id"], "resp2": resp2["id"]}


def test_parse_message_dispatches_by_provider() -> None:
    msg = parse_message("anthropic",
                        {"content": [{"type": "text", "text": "x"}]})
    assert msg == Message(role="assistant", content=(TextBlock(text="x"),))


def test_build_messages_reconstructs_full_turn_sequence() -> None:
    log = InMemoryEventLog()
    _agent_stream(log, "step:p/s")
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    roles = [m.role for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    # The synthesized tool-result wrapper is a user message of ToolResultBlocks
    assert isinstance(msgs[2].content[0], ToolResultBlock)
    assert msgs[2].content[0].content == "FILE"


def test_build_messages_filters_by_agent_id() -> None:
    log = InMemoryEventLog()
    _agent_stream(log, "step:p/s")
    _agent_stream(log, "sub:abc")
    only = build_messages_from_events(list(log.read()), agent_id="sub:abc")
    assert [m.role for m in only] == ["user", "assistant", "user", "assistant"]


def test_until_event_id_is_exclusive() -> None:
    log = InMemoryEventLog()
    ids = _agent_stream(log, "step:p/s")
    msgs = build_messages_from_events(
        list(log.read()), agent_id="step:p/s", until_event_id=ids["resp2"])
    # everything up to (not including) resp2: user, assistant, user(tool_result)
    assert [m.role for m in msgs] == ["user", "assistant", "user"]


def test_reconstruct_request_includes_system_tools_messages() -> None:
    log = InMemoryEventLog()
    ids = _agent_stream(log, "step:p/s")
    req = reconstruct_request_for_response(list(log.read()), ids["resp2"])
    assert req["system"] == "SYS"
    assert req["tools"] == [{"name": "read_file"}]
    assert req["model"] == "m"
    assert [m.role for m in req["messages"]] == ["user", "assistant", "user"]


def _batch_turn_stream(log: InMemoryEventLog, agent_id: str) -> None:
    # One assistant turn that issued tool_uses [t1, t2] in DOCUMENT order, whose
    # tool_result events then arrived in COMPLETION order [t2, t1] (the out-of-order
    # batch case). build_messages_from_events must reconstruct the user message in
    # tool_use (document) order: [t1, t2].
    log.append_step_context(
        agent_id=agent_id, parent_id_override="seed",
        provider="anthropic", model="m", system_prompt="SYS",
        tools_schema=[{"name": "sub_agent"}], params={}, max_tool_calls=50)
    log.append_user_message(
        agent_id=agent_id,
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    log.append_llm_response(
        agent_id=agent_id, provider="anthropic", model="m",
        raw={"content": [
            {"type": "tool_use", "id": "t1", "name": "sub_agent", "input": {"a": 1}},
            {"type": "tool_use", "id": "t2", "name": "sub_agent", "input": {"a": 2}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    # Completion order: t2 settles first, then t1.
    log.append_tool_result(agent_id=agent_id,
                           block=ToolResultBlock(tool_use_id="t2", content="R2"))
    log.append_tool_result(agent_id=agent_id,
                           block=ToolResultBlock(tool_use_id="t1", content="R1"))
    log.append_llm_response(
        agent_id=agent_id, provider="anthropic", model="m",
        raw={"content": [{"type": "text", "text": "done"}],
             "stop_reason": "end_turn", "usage": {}},
        usage={}, stop_reason="end_turn")


def test_build_messages_reorders_batch_results_to_tool_use_order() -> None:
    log = InMemoryEventLog()
    _batch_turn_stream(log, "step:p/s")
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    # The tool_result user message (msgs[2]) is reordered to tool_use order t1, t2,
    # NOT the completion order t2, t1 the events were logged in.
    ids = [b.tool_use_id for b in msgs[2].content]
    assert ids == ["t1", "t2"]


def test_build_messages_keeps_unmatched_results_at_tail_in_arrival_order() -> None:
    # A result whose tool_use_id is NOT in the preceding assistant turn keeps its
    # arrival position AFTER the matched, reordered results (design: "unmatched
    # results keep arrival order at the tail").
    log = InMemoryEventLog()
    log.append_step_context(
        agent_id="step:p/s", parent_id_override="seed",
        provider="anthropic", model="m", system_prompt="SYS",
        tools_schema=[{"name": "sub_agent"}], params={}, max_tool_calls=50)
    log.append_user_message(
        agent_id="step:p/s",
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    log.append_llm_response(
        agent_id="step:p/s", provider="anthropic", model="m",
        raw={"content": [
            {"type": "tool_use", "id": "t1", "name": "sub_agent", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "sub_agent", "input": {}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    log.append_tool_result(agent_id="step:p/s",
                           block=ToolResultBlock(tool_use_id="t2", content="R2"))
    log.append_tool_result(agent_id="step:p/s",
                           block=ToolResultBlock(tool_use_id="zz", content="RZ"))
    log.append_tool_result(agent_id="step:p/s",
                           block=ToolResultBlock(tool_use_id="t1", content="R1"))
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    ids = [b.tool_use_id for b in msgs[2].content]
    assert ids == ["t1", "t2", "zz"]


def test_repair_backfills_every_dangling_tool_use_not_just_the_last(tmp_path) -> None:
    # AC9: a suspended BATCH leaves an assistant message whose [task1, task2, task3]
    # tool_uses are ALL dangling (multiple dangling tool_uses, not a trailing one).
    # repair_dangling_tool_use_on_resume must synthesize a placeholder result for
    # EACH, restoring the tool_use<->tool_result balance for the whole batch.
    from imp.agents.transcript import Transcript, repair_dangling_tool_use_on_resume

    assistant = Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="sub_agent", input={}),
        ToolUseBlock(id="t2", name="sub_agent", input={}),
        ToolUseBlock(id="t3", name="sub_agent", input={})))
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        assistant]
    transcript = Transcript(tmp_path / "transcript.jsonl")
    synth = repair_dangling_tool_use_on_resume(messages, transcript)
    assert synth is not None
    repaired_ids = sorted(b.tool_use_id for b in synth)
    assert repaired_ids == ["t1", "t2", "t3"]


def test_build_messages_reconstructs_settled_subset_with_dangling_siblings() -> None:
    # AC9: a batch where task2 SETTLED (its tool_result event was logged) while
    # task1 and task3 dangle. Reconstruction emits exactly the settled subset's
    # result (no fabricated results for the dangling siblings) — the resume repair
    # path (pinned above) backfills the dangling ones separately.
    log = InMemoryEventLog()
    log.append_step_context(
        agent_id="step:p/s", parent_id_override="seed",
        provider="anthropic", model="m", system_prompt="SYS",
        tools_schema=[{"name": "sub_agent"}], params={}, max_tool_calls=50)
    log.append_user_message(
        agent_id="step:p/s",
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    log.append_llm_response(
        agent_id="step:p/s", provider="anthropic", model="m",
        raw={"content": [
            {"type": "tool_use", "id": "t1", "name": "sub_agent", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "sub_agent", "input": {}},
            {"type": "tool_use", "id": "t3", "name": "sub_agent", "input": {}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    # Only the middle sibling settled.
    log.append_tool_result(agent_id="step:p/s",
                           block=ToolResultBlock(tool_use_id="t2", content="R2"))
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    assert [m.role for m in msgs] == ["user", "assistant", "user"]
    settled = [b.tool_use_id for b in msgs[2].content]
    assert settled == ["t2"]


def test_trailing_dangling_tool_use_drops_empty_assistant_turn() -> None:
    log = InMemoryEventLog()
    log.append_user_message(
        agent_id="step:p/s",
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    log.append_llm_response(
        agent_id="step:p/s", provider="anthropic", model="m",
        raw={"content": [{"type": "tool_use", "id": "t1",
                          "name": "read_file", "input": {"p": "x"}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    # No tool_result event for t1 → the trailing tool_use is dangling.
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    # The assistant turn held ONLY the dangling tool_use, so it is dropped whole.
    assert [m.role for m in msgs] == ["user"]


def test_trailing_dangling_tool_use_drops_block_but_keeps_text() -> None:
    log = InMemoryEventLog()
    log.append_user_message(
        agent_id="step:p/s",
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    log.append_llm_response(
        agent_id="step:p/s", provider="anthropic", model="m",
        raw={"content": [{"type": "text", "text": "hold on"},
                         {"type": "tool_use", "id": "t1",
                          "name": "read_file", "input": {}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    # The assistant turn survives with its text; only the dangling block is gone.
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[-1].content == (TextBlock(text="hold on"),)


def test_well_formed_transcript_reconstructs_unchanged() -> None:
    log = InMemoryEventLog()
    _agent_stream(log, "step:p/s")     # every tool_use is matched by a tool_result
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    # The matched tool_use is preserved (not stripped) in the assistant turn.
    assert any(isinstance(b, ToolUseBlock) for b in msgs[1].content)


def test_stall_redrive_balances_torn_down_turn_no_replay(tmp_path) -> None:
    # D5 / AC7: a torn-down turn (unmatched tool_use tail) must be repaired
    # before re-drive, never replayed unbalanced.
    torn_down = Message(role="assistant", content=(
        TextBlock(text="calling a tool"),
        ToolUseBlock(id="t1", name="bash", input={"command": "sleep 99999"})))
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        torn_down]

    # Pre-repair: replaying this tail unbalanced is the 400-triggering shape.
    assert classify_resume(messages) is ResumeState.DANGLING_TOOL_USE

    transcript = Transcript(tmp_path / "transcript.jsonl")
    synth = repair_dangling_tool_use_on_resume(messages, transcript)

    assert synth is not None
    assert [b.tool_use_id for b in synth] == ["t1"]
    # Post-repair: the dangling tool_use is paired, so the re-drive sends a balanced transcript.
    assert classify_resume(messages) is not ResumeState.DANGLING_TOOL_USE
    assert classify_resume(messages) is ResumeState.SEND_TO_LLM


def test_trailing_dangling_tool_use_with_thinking_drops_whole_turn() -> None:
    log = InMemoryEventLog()
    log.append_user_message(
        agent_id="step:p/s",
        message=Message(role="user", content=(TextBlock(text="begin"),)))
    log.append_llm_response(
        agent_id="step:p/s", provider="anthropic", model="m",
        raw={"content": [{"type": "thinking", "thinking": "deciding",
                          "signature": "sig-x"},
                         {"type": "tool_use", "id": "t1",
                          "name": "read_file", "input": {}}],
             "stop_reason": "tool_use", "usage": {}},
        usage={}, stop_reason="tool_use")
    # t1 is never resolved → the trailing tool_use is dangling. After dropping
    # it the turn would be thinking-only, which is not a valid resend turn, so
    # the whole assistant message is dropped.
    msgs = build_messages_from_events(list(log.read()), agent_id="step:p/s")
    assert [m.role for m in msgs] == ["user"]
