"""Regression: an image tool result must survive the claude_code transport.

`_build_options` never set `max_buffer_size`, so claude_agent_sdk's subprocess
transport applied its 1 MiB per-stream-json-line default. The CLI echoes an
image tool result TWICE on one line (the content block plus a top-level
`tool_use_result` copy — measured ratio 2.0007 on live sessions), so a routine
~640 KB base64 terminal screenshot overran the limit, raised CLIJSONDecodeError,
fell past imp's classifier into make_unknown_error_event, and fatally ended the
step. The buffer is derived from imp's own base64 ceiling so raising one without
the other cannot silently reintroduce the failure.
"""

from __future__ import annotations

import json
from pathlib import Path

from imp.agents.runner import AgentInvocation, ImagePart, QuestContext
from imp.agents.tools.image import BASE64_TARGET_BYTES
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.claude_code import (
    _CLI_LINE_BUFFER_BYTES,
    ClaudeCodeAgentRunner,
    _tool_response_parts,
)

# Measured on claude CLI 2.1.263: a tool_result line is 2 * b64_len + ~480 bytes
# of JSON envelope (ids, session_id, uuid, timestamp). Rounded up for headroom.
_OBSERVED_LINE_OVERHEAD_BYTES = 1024


def _invocation(project_root: Path) -> AgentInvocation:
    loc = SourceLoc(file=Path("w.yaml"), line=1)
    agent_def = AgentDef(
        name="coder",
        description="",
        tools=frozenset({"bash"}),
        subagents=(),
        output_schema=OutputSchema(fields=(), source=loc),
        prompt_template_source="",
        source_path=Path("coder.md"),
        source=loc,
    )
    return AgentInvocation(
        agent_def=agent_def,
        provider_name="claude_code",
        model_name="claude-sonnet-4-5",
        system_prompt="be a coder",
        initial_messages=(),
        quest_context=QuestContext(
            title="Test",
            project_root=project_root,
            workflow_dir=project_state_dir(project_root) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=project_state_dir(project_root) / "steps",
        events_path=project_root / "events.jsonl",
    )


def test_build_options_carries_the_line_buffer(tmp_path: Path) -> None:
    """The constant existing is not enough — it has to reach the SDK options,
    or the transport keeps its 1 MiB default."""
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.max_buffer_size == _CLI_LINE_BUFFER_BYTES, (
        f"expected ClaudeAgentOptions.max_buffer_size == {_CLI_LINE_BUFFER_BYTES}, got "
        f"{opts.max_buffer_size!r}; None leaves claude_agent_sdk's 1 MiB per-line "
        f"default, which an image tool result overruns."
    )
    # The SDK's declared type is `int | None`; math.inf would satisfy the
    # equality above but is not a legal value.
    assert isinstance(opts.max_buffer_size, int)


def test_line_buffer_clears_the_cli_double_echo_of_the_image_ceiling() -> None:
    """Raising BASE64_TARGET_BYTES without raising the buffer must fail here."""
    assert _CLI_LINE_BUFFER_BYTES >= 2 * BASE64_TARGET_BYTES, (
        f"the CLI repeats an image tool result twice on one stream-json line, so the "
        f"buffer ({_CLI_LINE_BUFFER_BYTES}) must clear 2 * BASE64_TARGET_BYTES "
        f"({2 * BASE64_TARGET_BYTES})."
    )


def test_worst_case_image_tool_result_line_fits_the_buffer() -> None:
    """Wire arithmetic on the real serialized shape: an ImagePart at the base64
    ceiling, as _tool_response_parts emits it, echoed twice plus envelope."""
    part = ImagePart(
        media_type="image/png",
        data="A" * BASE64_TARGET_BYTES,
        width=2000,
        height=2000,
        blob="0" * 64 + ".png",
        nbytes=3_750_000,
    )
    serialized_len = len(json.dumps(_tool_response_parts([part])))
    line_len = 2 * serialized_len + _OBSERVED_LINE_OVERHEAD_BYTES
    assert line_len <= _CLI_LINE_BUFFER_BYTES, (
        f"a worst-case image tool result serializes to {serialized_len} bytes and lands "
        f"on the wire as a {line_len}-byte line, over the {_CLI_LINE_BUFFER_BYTES}-byte "
        f"buffer; the transport would raise CLIJSONDecodeError."
    )
