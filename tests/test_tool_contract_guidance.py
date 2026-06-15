from __future__ import annotations

from pathlib import Path

from imp.agents.tools.wait_for_user import make_wait_for_user_tool
from imp.engine.agent_run import _AGENT_BASE_PROMPT
from imp.engine.completion import completion_instruction
from imp.engine.types import OutputField, OutputSchema, SourceLoc


def _schema() -> OutputSchema:
    return OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def test_completion_instruction_states_args_are_hidden() -> None:
    text = completion_instruction(_schema())
    assert "not shown to the user" in text
    assert "prose" in text.lower()


def test_wait_for_user_description_takes_no_arguments() -> None:
    desc = make_wait_for_user_tool().description
    assert "this tool takes no arguments" in desc
    assert "Pass `prompt`" not in desc


def test_base_prompt_carries_state_once_rule() -> None:
    assert "not shown to the user" in _AGENT_BASE_PROMPT
