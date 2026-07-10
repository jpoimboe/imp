"""Part 1: NarrativeBuilder backoff-wait accounting (timer honesty)."""
from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev

TS0 = "2026-01-01T00:00:00+00:00"


def _step_builder() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    b.add(ev("1", "change_created", quest_id=1, title="x"))
    b.add(ev("2", "step_started", step="review", kind="agent", phase="review",
             phase_label="review phase", label="reviewing", model="m",
             timestamp=TS0))
    return b


def test_no_retry_yields_zero_wait() -> None:
    b = _step_builder()
    assert b.backoff_wait_seconds("2026-01-01T00:00:10+00:00") == 0.0


def test_single_completed_backoff_span_accumulates() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="auth", attempt=1,
             backoff_seconds=8.0, timestamp="2026-01-01T00:00:05+00:00"))
    # A resuming event ends the wait 8s later (05 .. 13).
    b.add(ev("4", "thinking", content="back",
             timestamp="2026-01-01T00:00:13+00:00"))
    assert b.backoff_wait_seconds("2026-01-01T00:00:20+00:00") == 8.0


def test_multiple_sequential_spans_sum() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="auth", attempt=1,
             backoff_seconds=8.0, timestamp="2026-01-01T00:00:05+00:00"))
    b.add(ev("4", "thinking", content="a",
             timestamp="2026-01-01T00:00:13+00:00"))          # +8 (05..13)
    b.add(ev("5", "agent_retry", category="network", attempt=2,
             backoff_seconds=16.0, timestamp="2026-01-01T00:00:20+00:00"))
    b.add(ev("6", "tool_call", id="z", name="read_file", input={},
             timestamp="2026-01-01T00:00:30+00:00"))          # +10 (20..30)
    assert b.backoff_wait_seconds("2026-01-01T00:00:40+00:00") == 18.0


def test_in_progress_span_resolved_against_reference() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="rate_limit", attempt=1,
             backoff_seconds=30.0, timestamp="2026-01-01T00:00:05+00:00"))
    # No resuming event yet: the live reference resolves the open span.
    assert b.backoff_wait_seconds("2026-01-01T00:00:12+00:00") == 7.0
    assert b.backoff_wait_seconds("2026-01-01T00:00:25+00:00") == 20.0


def test_non_backoff_category_contributes_nothing() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="config", attempt=1,
             backoff_seconds=8.0, timestamp="2026-01-01T00:00:05+00:00"))
    b.add(ev("4", "thinking", content="x",
             timestamp="2026-01-01T00:00:13+00:00"))
    assert b.backoff_wait_seconds("2026-01-01T00:00:20+00:00") == 0.0


def test_stalled_category_accounts_wait() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="stalled", attempt=1,
             backoff_seconds=4.0, timestamp="2026-01-01T00:00:05+00:00"))
    b.add(ev("4", "thinking", content="x",
             timestamp="2026-01-01T00:00:09+00:00"))
    assert b.backoff_wait_seconds("2026-01-01T00:00:20+00:00") == 4.0


def test_consecutive_retries_without_resume_stay_one_span() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="auth", attempt=1,
             backoff_seconds=8.0, timestamp="2026-01-01T00:00:05+00:00"))
    # A second retry with no resuming event between must NOT re-open / restart
    # the span — the earliest start is kept so the whole idle stretch counts once.
    b.add(ev("4", "agent_retry", category="auth", attempt=2,
             backoff_seconds=16.0, timestamp="2026-01-01T00:00:13+00:00"))
    assert b.backoff_wait_seconds("2026-01-01T00:00:25+00:00") == 20.0


def test_step_backoff_attributed_to_stepnode() -> None:
    b = _step_builder()
    b.add(ev("3", "agent_retry", category="auth", attempt=1,
             backoff_seconds=8.0, timestamp="2026-01-01T00:00:05+00:00"))
    b.add(ev("4", "thinking", content="x",
             timestamp="2026-01-01T00:00:13+00:00"))
    assert b._step_stack[-1].backoff_wait_s == 8.0


def test_collapsed_step_line_excludes_backoff_wait() -> None:
    from imp.cli.render_text import collapsed_step_line
    node = nv.StepNode(name="review", kind="agent", label="reviewing",
                       start_ts="2026-01-01T00:00:00+00:00",
                       end_ts="2026-01-01T00:00:40+00:00",
                       backoff_wait_s=30.0, closed=True)
    line = collapsed_step_line(node, depth=0, color=False)
    assert "· 10s" in line     # 40s wall - 30s wait
    assert "40s" not in line


def test_top_level_step_breadcrumb_excludes_backoff_wait() -> None:
    from imp.cli.render_text import render_tree
    events = [
        ev("1", "change_created", quest_id=1, title="x"),
        ev("2", "step_started", step="review", kind="agent", phase="review",
           phase_label="review phase", label="reviewing", model="m",
           timestamp="2026-01-01T00:00:00+00:00"),
        ev("3", "thinking", agent_id="step:review/review", content="hmm",
           timestamp="2026-01-01T00:00:01+00:00"),
        ev("4", "agent_retry", category="auth", attempt=1, backoff_seconds=30.0,
           timestamp="2026-01-01T00:00:02+00:00"),
        ev("5", "thinking", agent_id="step:review/review", content="back",
           timestamp="2026-01-01T00:00:32+00:00"),          # +30 wait (02..32)
        ev("6", "step_completed", step="review", kind="agent",
           action={"verb": "continue"}, var_mutations={},
           timestamp="2026-01-01T00:00:40+00:00"),
    ]
    tree = nv.build_tree(events)
    body = "\n".join(render_tree(tree, full=False, color=False, width=80))
    assert "10s" in body       # 40s wall - 30s wait, on the step breadcrumb
    assert "40s" not in body


def test_subagent_closed_row_excludes_backoff_wait() -> None:
    from imp.cli.render_text import subagent_status_row
    nested = nv.NarrativeBuilder()
    nested.add(ev("r1", "agent_retry", category="auth", attempt=1,
                  backoff_seconds=30.0,
                  timestamp="2026-01-01T00:00:02+00:00"))
    # Fold closes mid-wait: no resuming event, so the open span resolves at end_ts.
    fold = nv.SubagentFold(
        subagent_name="code-reviewer", event_id="t1", block_id="b1",
        nested=nested, running=False, terminal_state="interrupted",
        start_ts="2026-01-01T00:00:00+00:00",
        end_ts="2026-01-01T00:00:14+00:00")
    row = subagent_status_row(fold, depth=0, color=False)
    # 14s wall - 12s wait (02..14) = 2s honest working time, not 14s.
    assert "· 2s" in row
    assert "14s" not in row
