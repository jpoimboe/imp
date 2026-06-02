from __future__ import annotations

from pathlib import Path

from imp.engine.completion import NUDGE_LIMIT, build_continuation_fn, completion_instruction
from imp.engine.types import OutputField, OutputSchema, SourceLoc


def _schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="verdict", type="enum",
                        values=("approve", "rework"), required=True),
            OutputField(name="notes", type="string", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def test_names_the_tool() -> None:
    assert "complete_step" in completion_instruction(_schema())


def test_names_every_required_field_and_enum_value() -> None:
    text = completion_instruction(_schema())
    for token in ("status", "Success", "Error", "verdict", "approve", "rework"):
        assert token in text, token


def test_marks_optional_fields_optional() -> None:
    text = completion_instruction(_schema())
    assert "notes" in text
    assert "optional" in text.lower()


def test_instructs_tool_call_not_prose() -> None:
    text = completion_instruction(_schema())
    assert "prose" in text.lower()


def test_renders_non_enum_types() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="count", type="int", required=True),
            OutputField(name="found", type="bool", required=True),
            OutputField(name="payload", type="object", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    text = completion_instruction(schema)
    assert "`count` (int)" in text
    assert "`found` (bool)" in text
    assert "`payload` (object)" in text
    assert "optional" in text.lower()  # payload is optional


def _instruction() -> str:
    return "To finish this step you MUST call the `complete_step` tool with X."


def test_nudge_fn_returns_instruction_then_none() -> None:
    fn = build_continuation_fn(
        interactive=False, user_chat_input_fn=None,
        instruction=_instruction(), log=None, agent_id="step:p/s")
    assert fn is not None
    # First NUDGE_LIMIT calls return the corrective text...
    for _ in range(NUDGE_LIMIT):
        out = fn("some prose")
        assert out is not None
        assert "complete_step" in out
    # ...then None (which the provider turns into agent_incomplete).
    assert fn("more prose") is None


def test_interactive_fn_emits_awaiting_then_reads_input() -> None:
    emitted: list[dict] = []

    class _Log:
        def append(self, *, agent_id, type, payload):
            emitted.append({"agent_id": agent_id, "type": type, "payload": payload})

    fn = build_continuation_fn(
        interactive=True, user_chat_input_fn=lambda _p: "user reply",
        instruction=_instruction(), log=_Log(), agent_id="step:p/s")
    assert fn is not None
    reply = fn("the agent's question")
    assert reply == "user reply"
    assert emitted == [{
        "agent_id": "step:p/s", "type": "awaiting_user_input",
        "payload": {"prompt": "the agent's question"}}]


def test_interactive_without_input_fn_is_none() -> None:
    fn = build_continuation_fn(
        interactive=True, user_chat_input_fn=None,
        instruction=_instruction(), log=None, agent_id="x")
    assert fn is None
