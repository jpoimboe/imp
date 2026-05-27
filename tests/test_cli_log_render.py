"""Tests for the imp log renderer (src/imp/cli/render.py)."""

from __future__ import annotations

import io

import pytest

from imp.cli import render


def test_style_disabled_returns_plain_text() -> None:
    assert render._style("hi", "red", enabled=False) == "hi"


def test_style_enabled_wraps_in_ansi() -> None:
    out = render._style("hi", "red", enabled=True)
    assert out == "\x1b[31mhi\x1b[0m"


def test_style_multiple_codes() -> None:
    out = render._style("hi", "bold", "magenta", enabled=True)
    # codes concatenated in order, single reset
    assert out == "\x1b[1m\x1b[35mhi\x1b[0m"


def test_should_use_color_off_when_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()  # not a tty
    assert render.should_use_color(stream) is False


def test_should_use_color_off_when_no_color_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    assert render.should_use_color(FakeTTY()) is False


def test_style_no_codes_returns_plain_text() -> None:
    assert render._style("hi", enabled=True) == "hi"


def test_should_use_color_on_when_tty_and_no_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    assert render.should_use_color(FakeTTY()) is True


def test_fmt_change_created() -> None:
    s = render._fmt_change_created(
        {"change_id": 42, "title": "add foo to bar"}, full=False, color=False,
    )
    assert s == "#42  add foo to bar"


def test_fmt_workflow_started() -> None:
    s = render._fmt_workflow_started(
        {"name": "standard-feature", "from_phase": "onboard"},
        full=False, color=False,
    )
    assert s == "standard-feature  (from onboard)"


def test_fmt_workflow_paused_with_reason() -> None:
    s = render._fmt_workflow_paused(
        {"reason": "ctrl-c"}, full=False, color=False,
    )
    assert s == "paused  (ctrl-c)"


def test_fmt_workflow_paused_no_reason() -> None:
    # Defensive: payload may be {} per executor.py line 100.
    s = render._fmt_workflow_paused({}, full=False, color=False)
    assert s == "paused"


def test_fmt_workflow_resumed() -> None:
    s = render._fmt_workflow_resumed({}, full=False, color=False)
    assert s == "resumed"


def test_fmt_phase_started() -> None:
    s = render._fmt_phase_started({"phase": "design"}, full=False, color=False)
    assert s == "design"


def test_fmt_phase_completed_no_color() -> None:
    s = render._fmt_phase_completed(
        {"phase": "design", "action": "next"}, full=False, color=False,
    )
    assert s == "design  → next"


def test_fmt_phase_completed_color_accents_action() -> None:
    s = render._fmt_phase_completed(
        {"phase": "design", "action": "loop"}, full=False, color=True,
    )
    # arrow dimmed, action green
    assert "\x1b[2m→\x1b[0m" in s
    assert "\x1b[32mloop\x1b[0m" in s


def test_fmt_phase_exhausted() -> None:
    s = render._fmt_phase_exhausted({"phase": "design"}, full=False, color=False)
    assert s == "design  → exhausted"


def test_fmt_phase_exhausted_color() -> None:
    s = render._fmt_phase_exhausted({"phase": "design"}, full=False, color=True)
    assert "\x1b[31mexhausted\x1b[0m" in s


def test_fmt_step_started_basic() -> None:
    s = render._fmt_step_started(
        {"step": "spec-writer", "model_role": "expensive", "interactive": False},
        full=False, color=False,
    )
    assert s == "spec-writer  [expensive]"


def test_fmt_step_started_omits_default_role() -> None:
    s = render._fmt_step_started(
        {"step": "spec-writer", "model_role": "default", "interactive": False},
        full=False, color=False,
    )
    assert s == "spec-writer"


def test_fmt_step_started_missing_role_treated_as_default() -> None:
    # Older events may omit model_role entirely; suppress the bracket too.
    s = render._fmt_step_started(
        {"step": "spec-writer", "interactive": False},
        full=False, color=False,
    )
    assert s == "spec-writer"


def test_fmt_step_started_interactive_suffix() -> None:
    s = render._fmt_step_started(
        {"step": "talk", "model_role": "default", "interactive": True},
        full=False, color=False,
    )
    assert s == "talk (interactive)"


def test_fmt_step_completed() -> None:
    s = render._fmt_step_completed(
        {"step": "spec-writer", "action": "continue"}, full=False, color=False,
    )
    assert s == "spec-writer  → continue"


def test_fmt_step_exhausted() -> None:
    s = render._fmt_step_exhausted(
        {"step": "spec-writer", "attempts": 4}, full=False, color=False,
    )
    assert s == "spec-writer  → exhausted"


def test_fmt_step_resumed_with_cached_output() -> None:
    s = render._fmt_step_resumed_with_cached_output(
        {"step": "spec-writer"}, full=False, color=False,
    )
    assert s == "spec-writer  (cached)"


def test_fmt_gate_evaluated_modern_with_gate_name() -> None:
    s = render._fmt_gate_evaluated(
        {"kind": "agent", "gate": "spec-lint",
         "action": {"kind": "continue"}},
        full=False, color=False,
    )
    assert s == "spec-lint (agent)  → continue"


def test_fmt_gate_evaluated_modern_without_gate_name() -> None:
    s = render._fmt_gate_evaluated(
        {"kind": "human", "action": {"kind": "retry"}},
        full=False, color=False,
    )
    assert s == "human gate  → retry"


def test_fmt_gate_evaluated_modern_goto_target() -> None:
    s = render._fmt_gate_evaluated(
        {"kind": "agent", "gate": "review",
         "action": {"kind": "goto", "target": "implement"}},
        full=False, color=False,
    )
    assert s == "review (agent)  → goto(implement)"


def test_fmt_gate_evaluated_legacy_shape() -> None:
    s = render._fmt_gate_evaluated(
        {"phase": "onboard", "gate_type": "HumanGate", "action": "continue"},
        full=False, color=False,
    )
    assert s == "HumanGate on onboard  → continue"


def test_fmt_gate_evaluated_legacy_on_step() -> None:
    s = render._fmt_gate_evaluated(
        {"step": "spec-writer", "gate_type": "ScriptGate", "action": "retry"},
        full=False, color=False,
    )
    assert s == "ScriptGate on spec-writer  → retry"


def test_fmt_script_gate_result_success() -> None:
    s = render._fmt_script_gate_result(
        {"exit": 0, "stdout": "ok", "stderr": ""}, full=False, color=False,
    )
    assert s == "→ exit=0"


def test_fmt_script_gate_result_failure_with_stderr() -> None:
    s = render._fmt_script_gate_result(
        {"exit": 1, "stdout": "", "stderr": "boom: file missing\nignored line two"},
        full=False, color=False,
    )
    assert s == "→ exit=1  boom: file missing"


def test_fmt_script_gate_result_failure_long_stderr_elided() -> None:
    long = "x" * 200
    s = render._fmt_script_gate_result(
        {"exit": 2, "stdout": "", "stderr": long}, full=False, color=False,
    )
    # 60 chars then ellipsis token
    assert "…" in s
    assert s.startswith("→ exit=2  ")


def test_fmt_agent_output_two_keys_truncated() -> None:
    s = render._fmt_agent_output(
        {"status": "Success", "summary": "x" * 100, "extra": "y"},
        full=False, color=False,
    )
    # first two pairs only; summary value truncated to 60
    assert s.startswith("status=Success summary=")
    assert "extra" not in s  # third key omitted
    assert "…" in s


def test_fmt_error_truncates_to_80() -> None:
    s = render._fmt_error({"message": "z" * 200}, full=False, color=False)
    assert len(s) <= 81  # 80 chars + ellipsis token
    assert "…" in s


def test_fmt_tools_synthetic() -> None:
    s = render._fmt_tools(
        {"counts": {"bash": 3, "ls": 1, "read": 2}}, full=False, color=False,
    )
    # total = 6, sorted by count desc
    assert s == "6 calls  (3 bash, 2 read, 1 ls)"


def test_fmt_unknown_renders_kv_pairs() -> None:
    s = render._fmt_unknown(
        {"a": "alpha", "b": "b" * 100}, full=False, color=False,
    )
    assert s.startswith("a=alpha b=")
    assert "…" in s


def test_fmt_malformed() -> None:
    s = render._fmt_malformed({"not": "an event"}, full=False, color=False)
    assert s.startswith("{")  # repr of the raw object
    assert len(s) <= 80


def test_fmt_tool_call_args_keyvalue() -> None:
    s = render._fmt_tool_call(
        {"id": "tu_1", "name": "bash", "args": {"command": "ls", "timeout": 5}},
        full=True, color=False,
    )
    assert s.startswith("bash  (")
    assert "command=ls" in s
    assert "timeout=5" in s


def test_fmt_tool_call_long_arg_elided() -> None:
    long = "x" * 200
    s = render._fmt_tool_call(
        {"id": "tu_1", "name": "bash", "args": {"command": long}},
        full=True, color=False,
    )
    assert "…" in s


def test_fmt_tool_result_with_known_name() -> None:
    # Result formatter takes the resolved tool_name as a parameter.
    s = render._fmt_tool_result_with_name(
        {"tool_call_id": "tu_1", "result": "ok\nhello"},
        tool_name="bash", full=True, color=False,
    )
    # newlines escaped, no elision needed
    assert s == "bash → ok\\nhello"


def test_fmt_tool_result_long_content_elided_with_charcount() -> None:
    long = "y" * 500
    s = render._fmt_tool_result_with_name(
        {"tool_call_id": "tu_1", "result": long},
        tool_name="bash", full=True, color=False,
    )
    assert s.startswith("bash → ")
    assert "… (500 chars)" in s


def test_fmt_tool_result_unknown_name_renders_tool() -> None:
    # When the resolver couldn't find a name (orphan tool_result), the
    # literal label "tool" is used.
    s = render._fmt_tool_result_with_name(
        {"tool_call_id": "ghost", "result": "x"},
        tool_name=None, full=True, color=False,
    )
    assert s == "tool → x"


def test_resolve_tool_name_builds_running_map() -> None:
    events = [
        {"type": "tool_call",
         "payload": {"id": "tu_1", "name": "bash", "args": {}}},
        {"type": "tool_result",
         "payload": {"tool_call_id": "tu_1", "result": "ok"}},
        {"type": "tool_call",
         "payload": {"id": "tu_2", "name": "read", "args": {}}},
    ]
    seen: dict[str, str | None] = {}
    for e in events:
        if e["type"] == "tool_result":
            name = render._lookup_tool_name(e["payload"].get("tool_call_id"), seen)
            assert name == "bash"
        elif e["type"] == "tool_call":
            render._record_tool_call(e["payload"], seen)
    assert seen == {"tu_1": "bash", "tu_2": "read"}
