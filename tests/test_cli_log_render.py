"""Tests for the imp log renderer (src/imp/cli/render.py)."""

from __future__ import annotations

import io
from pathlib import Path

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


def test_fmt_human_gate_pending() -> None:
    s = render._fmt_human_gate_pending(
        {"phase": "review", "prompt": "Merge?",
         "options": [{"label": "Merge"}, {"label": "Back to implement"}]},
        full=False, color=False,
    )
    assert s == "review prompt='Merge?' (2 options)"


def test_fmt_human_gate_pending_singular_option() -> None:
    s = render._fmt_human_gate_pending(
        {"phase": "spec", "prompt": "Continue?",
         "options": [{"label": "Yes"}]},
        full=False, color=False,
    )
    assert s == "spec prompt='Continue?' (1 option)"


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


def test_fmt_tools_synthetic_singular() -> None:
    s = render._fmt_tools(
        {"counts": {"bash": 1}}, full=False, color=False,
    )
    assert s == "1 call  (1 bash)"


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


def test_fmt_malformed_recognizes_sentinel_dict() -> None:
    """When the input is the EventLog malformed sentinel, render the raw
    line content, not the dict's repr."""
    s = render._fmt_malformed(
        {"_malformed": True, "raw": "this is not json"},
        full=False, color=False,
    )
    assert s == "malformed line: this is not json"


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


def _ev(t: str, ts: str = "2026-05-27T10:00:00+00:00", **p) -> dict:
    return {"timestamp": ts, "type": t, "payload": dict(p)}


def test_collapse_happy_path_one_burst() -> None:
    events = [
        _ev("step_started", step="s1"),
        _ev("tool_call", id="t1", name="bash", args={}),
        _ev("tool_result", tool_call_id="t1", result="ok"),
        _ev("tool_call", id="t2", name="bash", args={}),
        _ev("tool_result", tool_call_id="t2", result="ok"),
        _ev("tool_call", id="t3", name="ls",  args={}, ts="2026-05-27T10:00:09+00:00"),
        _ev("tool_result", tool_call_id="t3", result="ok",
            ts="2026-05-27T10:00:10+00:00"),
        _ev("step_completed", step="s1", action="continue"),
    ]
    out = list(render._collapse_tool_bursts(events))
    # step_started, tools, step_completed = 3 events
    assert [e["type"] for e in out] == ["step_started", "tools", "step_completed"]
    tools = out[1]
    assert tools["payload"]["counts"] == {"bash": 2, "ls": 1}
    # synthetic event inherits last tool event's timestamp
    assert tools["timestamp"] == "2026-05-27T10:00:10+00:00"


def test_collapse_split_by_non_tool_event() -> None:
    events = [
        _ev("step_started", step="s1"),
        _ev("tool_call", id="t1", name="bash", args={}),
        _ev("tool_result", tool_call_id="t1", result="ok"),
        _ev("gate_evaluated", kind="script", action={"kind": "continue"}),
        _ev("tool_call", id="t2", name="bash", args={}),
        _ev("tool_result", tool_call_id="t2", result="ok"),
        _ev("step_completed", step="s1", action="continue"),
    ]
    out = list(render._collapse_tool_bursts(events))
    types = [e["type"] for e in out]
    assert types == [
        "step_started", "tools", "gate_evaluated", "tools", "step_completed",
    ]


def test_collapse_no_step_window_no_collapse() -> None:
    # Outside step_started..step_completed, tool events pass through unchanged
    # (current engine never emits them there, but the renderer should be safe).
    events = [
        _ev("tool_call", id="t1", name="bash", args={}),
        _ev("tool_result", tool_call_id="t1", result="ok"),
    ]
    out = list(render._collapse_tool_bursts(events))
    assert [e["type"] for e in out] == ["tool_call", "tool_result"]


def test_collapse_burst_split_by_workflow_pause() -> None:
    events = [
        _ev("step_started", step="s1"),
        _ev("tool_call", id="t1", name="bash", args={}),
        _ev("workflow_paused", reason="ctrl-c"),
        _ev("workflow_resumed"),
        _ev("tool_call", id="t2", name="bash", args={}),
        _ev("tool_result", tool_call_id="t2", result="ok"),
        _ev("step_completed", step="s1", action="continue"),
    ]
    out = list(render._collapse_tool_bursts(events))
    types = [e["type"] for e in out]
    # First burst: just t1 → one tools. Then pause/resume. Then second burst.
    assert types == [
        "step_started", "tools",
        "workflow_paused", "workflow_resumed",
        "tools", "step_completed",
    ]


def test_collapse_empty_iterable() -> None:
    assert list(render._collapse_tool_bursts(iter([]))) == []


def test_render_log_emits_one_line_per_event_default_mode() -> None:
    events = [
        _ev("change_created", change_id=1, title="hi"),
        _ev("workflow_started", name="wf", from_phase="onboard"),
        _ev("phase_started", phase="onboard"),
        _ev("step_started", step="s1", model_role="default", interactive=False),
        _ev("step_completed", step="s1", action="continue"),
        _ev("phase_completed", phase="onboard", action="next"),
    ]
    lines = list(render.render_log(events, full=False, color=False))
    assert len(lines) == 6


def test_render_log_indentation_by_depth() -> None:
    events = [
        _ev("phase_started", phase="onboard"),
        _ev("step_started", step="s1", model_role="default", interactive=False),
        _ev("agent_output", status="ok"),
    ]
    lines = list(render.render_log(events, full=False, color=False))
    # phase_started: 0 spaces before "phase_started"
    assert lines[0].split("  ", 1)[1].startswith("phase_started")
    # step_started: 2 spaces before "step_started" within type column
    after_ts_1 = lines[1].split("  ", 1)[1]
    assert after_ts_1.startswith("  step_started")
    # agent_output: 4 spaces
    after_ts_2 = lines[2].split("  ", 1)[1]
    assert after_ts_2.startswith("    agent_output")


def test_render_log_single_day_uses_short_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "UTC")
    import time
    time.tzset()
    events = [
        _ev("change_created", "2026-05-27T10:00:00+00:00",
            change_id=1, title="hi"),
        _ev("phase_started", "2026-05-27T11:00:00+00:00", phase="x"),
    ]
    lines = list(render.render_log(events, full=False, color=False))
    # Local-time hour may differ from UTC, but format must be HH:MM:SS (8 chars).
    ts0 = lines[0].split("  ", 1)[0]
    assert len(ts0) == 8
    assert ts0[2] == ":" and ts0[5] == ":"


def test_render_log_multi_day_uses_long_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "UTC")
    import time
    time.tzset()
    events = [
        _ev("change_created", "2026-05-26T10:00:00+00:00",
            change_id=1, title="hi"),
        _ev("phase_started", "2026-05-28T10:00:00+00:00", phase="x"),
    ]
    lines = list(render.render_log(events, full=False, color=False))
    ts0 = lines[0].split("  ", 1)[0]
    # MM-DD HH:MM:SS = 14 chars
    assert len(ts0) == 14
    assert ts0[2] == "-" and ts0[5] == " "


def test_render_log_color_off_has_no_ansi() -> None:
    events = [_ev("change_created", change_id=1, title="hi")]
    line = next(iter(render.render_log(events, full=False, color=False)))
    assert "\x1b[" not in line


def test_render_log_color_on_styles_event_type() -> None:
    events = [_ev("change_created", change_id=1, title="hi")]
    line = next(iter(render.render_log(events, full=True, color=True)))
    # change_created is bold magenta
    assert "\x1b[1m" in line and "\x1b[35m" in line


def test_render_log_full_mode_shows_every_tool_event() -> None:
    events = [
        _ev("step_started", step="s1", model_role="default", interactive=False),
        _ev("tool_call", id="t1", name="bash", args={"command": "ls"}),
        _ev("tool_result", tool_call_id="t1", result="ok"),
        _ev("step_completed", step="s1", action="continue"),
    ]
    lines = list(render.render_log(events, full=True, color=False))
    # one line per raw event, no collapse
    assert len(lines) == 4
    assert "tool_call" in lines[1]
    assert "tool_result" in lines[2]
    # tool_result labels with the call's tool name
    assert "bash →" in lines[2]


def test_render_log_default_mode_collapses_tool_burst() -> None:
    events = [
        _ev("step_started", step="s1", model_role="default", interactive=False),
        _ev("tool_call", id="t1", name="bash", args={"command": "ls"}),
        _ev("tool_result", tool_call_id="t1", result="ok"),
        _ev("tool_call", id="t2", name="bash", args={"command": "pwd"}),
        _ev("tool_result", tool_call_id="t2", result="/"),
        _ev("step_completed", step="s1", action="continue"),
    ]
    lines = list(render.render_log(events, full=False, color=False))
    # step_started, tools (rolled-up), step_completed
    assert len(lines) == 3
    assert "tools" in lines[1]
    assert "2 calls" in lines[1]


def test_render_log_unknown_event_falls_back() -> None:
    events = [_ev("never_heard_of_this", foo="bar")]
    lines = list(render.render_log(events, full=False, color=False))
    assert len(lines) == 1
    assert "never_heard_of_this" in lines[0]
    assert "foo=bar" in lines[0]


def test_render_log_malformed_event_renders_and_continues() -> None:
    events = [
        {"type": "change_created", "payload": {"change_id": 1, "title": "ok"}},
        {"not_a_real_event": True},  # missing "type" — malformed
        {"type": "phase_started", "payload": {"phase": "x"}},
    ]
    lines = list(render.render_log(events, full=False, color=False))
    assert len(lines) == 3
    assert "malformed" in lines[1]
    # Iteration continued to the third event.
    assert "phase_started" in lines[2]


def test_render_log_non_dict_event_renders_as_malformed() -> None:
    events = [
        {"type": "change_created", "payload": {"change_id": 1, "title": "ok"}},
        None,  # not a dict at all — must not crash
        {"type": "phase_started", "payload": {"phase": "x"}},
    ]
    lines = list(render.render_log(events, full=False, color=False))
    assert len(lines) == 3
    assert "malformed" in lines[1]
    assert "phase_started" in lines[2]


def test_render_log_empty_yields_nothing() -> None:
    assert list(render.render_log([], full=False, color=False)) == []


def test_render_log_orphan_tool_result_renders_as_literal_tool() -> None:
    events = [
        _ev("step_started", step="s1", model_role="default", interactive=False),
        _ev("tool_result", tool_call_id="ghost", result="orphan"),
        _ev("step_completed", step="s1", action="continue"),
    ]
    lines = list(render.render_log(events, full=True, color=False))
    # full mode, no preceding tool_call → label "tool"
    assert "tool →" in lines[1]


def test_cli_log_pipes_through_render(
    tmp_path, monkeypatch, capsys,
) -> None:
    """End-to-end: ``imp log`` reads events.jsonl and prints rendered lines.

    Constructs a minimal Change directory by hand (no engine spinup) and
    invokes ``main([..])`` directly.
    """
    import json
    from imp.cli.main import main

    project = tmp_path
    (project / ".imp" / "changes" / "1").mkdir(parents=True)
    (project / ".imp" / "changes" / "1" / "change.json").write_text(
        json.dumps({"id": 1, "title": "smoke"}),
    )
    (project / ".imp" / "changes" / "1" / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "smoke"},
        }) + "\n"
        + json.dumps({
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "phase_started",
            "payload": {"phase": "onboard"},
        }) + "\n",
    )
    # ``_cmd_log`` only invokes ``Change.load`` (which reads change.json) and
    # ``change.event_log().read()`` — no state files needed.
    monkeypatch.chdir(project)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "change_created" in out
    assert "smoke" in out
    assert "phase_started" in out
    assert "onboard" in out
    # No raw JSON braces in default rendering
    assert "{" not in out


FIXTURES = Path(__file__).parent / "fixtures"


def _read_sample_events() -> list[dict]:
    import json
    src = FIXTURES / "log_events_sample.jsonl"
    out: list[dict] = []
    for line in src.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def test_snapshot_default_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    import time
    time.tzset()
    events = _read_sample_events()
    actual = "\n".join(render.render_log(events, full=False, color=False)) + "\n"
    golden = (FIXTURES / "log_default.txt").read_text()
    assert actual == golden, (
        "Default-mode rendering drift. To accept the new output, "
        "rewrite tests/fixtures/log_default.txt with the actual output."
    )


def test_snapshot_full_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    import time
    time.tzset()
    events = _read_sample_events()
    actual = "\n".join(render.render_log(events, full=True, color=False)) + "\n"
    golden = (FIXTURES / "log_full.txt").read_text()
    assert actual == golden, (
        "Full-mode rendering drift. To accept the new output, "
        "rewrite tests/fixtures/log_full.txt with the actual output."
    )


def test_imp_log_skips_malformed_lines_in_events_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage line in events.jsonl renders as a malformed marker; the
    surrounding good events still render and the command exits 0."""
    import json
    from imp.cli.main import main

    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "smoke"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "smoke"},
        }) + "\n"
        + "this is not json\n"
        + json.dumps({
            "timestamp": "2026-05-27T10:00:02+00:00",
            "type": "phase_started",
            "payload": {"phase": "onboard"},
        }) + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    from contextlib import redirect_stdout
    import io
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log", "1"])
    assert rc == 0
    out = buf.getvalue()
    assert "change_created" in out
    assert "malformed" in out
    assert "this is not json" in out  # raw line surfaced
    assert "phase_started" in out
    # Three rendered lines, one per event (good, malformed, good).
    assert out.count("\n") == 3


def test_cli_log_subprocess_no_color_on_pipe(tmp_path: Path) -> None:
    """`python -m imp log 1` against a real change dir, stdout piped → no ANSI.

    Covers the `__main__.py → _cmd_log` wiring, `should_use_color` on a
    non-tty stream, and the real `sys.exit` path."""
    import json, subprocess, sys

    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "sub"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "sub"},
        }) + "\n"
        + json.dumps({
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "phase_started",
            "payload": {"phase": "spec"},
        }) + "\n",
    )

    out = subprocess.run(
        [sys.executable, "-m", "imp", "log", "1"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert out.returncode == 0, (
        f"stderr: {out.stderr!r}\nstdout: {out.stdout!r}"
    )
    assert "change_created" in out.stdout
    assert "phase_started" in out.stdout
    # Pipe → not a tty → no ANSI codes regardless of NO_COLOR.
    assert "\x1b[" not in out.stdout
