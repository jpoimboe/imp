"""Fix A: agent_retry renders inline (self-healed retry visibility)."""

from __future__ import annotations

import json
from typing import Any

from imp.cli import narrative as nv
from imp.cli.narrative import NarrativeBuilder


def test_agent_retry_handler_registered() -> None:
    assert "agent_retry" in NarrativeBuilder._HANDLERS


def test_agent_retry_renders_category_and_attempt() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "network", "attempt": 1, "backoff_seconds": 1.0}},
    ])
    blob = json.dumps(nv.structural_repr(tree.root), default=str)
    assert "network" in blob
    assert "retry" in blob.lower()


def test_stalled_agent_retry_renders_verification_breadcrumb() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "stalled", "attempt": 2, "backoff_seconds": 4.0}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "warning: resuming — verifying prior work, not redoing it" in out
    assert "[stalled] retry" not in out   # the old technical retry line is gone
    assert "error:" not in out
    assert "[config]" not in out


def test_fmt_hm_renders_hours_and_minutes() -> None:
    from imp.cli.narrative import _fmt_hm

    # 8000s = 133 min = 2h 13m
    assert _fmt_hm(8000) == "2h 13m"
    # 7200s = 120 min = exactly 2h 0m (hours shown even when minutes are 0)
    assert _fmt_hm(7200) == "2h 0m"
    # 2700s = 45 min, under an hour -> minutes only, no "0h"
    assert _fmt_hm(2700) == "45m"
    # exactly the 120s threshold -> 2m, no hour segment
    assert _fmt_hm(120) == "2m"


def test_hinted_agent_retry_renders_reset_wait_line() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 1,
                     "backoff_seconds": 8000.0, "hinted": True}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "[rate_limit] waiting for limit reset — retry 1 in 2h 13m" in out
    assert "[rate_limit] retry 1 (8000.0s)" not in out   # the plain line must NOT appear


def test_long_backoff_without_hint_flag_renders_reset_wait_line() -> None:
    from imp.cli.render_text import render_tree

    # backoff_seconds >= 120 alone triggers the honest line even with no hinted flag.
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 2,
                     "backoff_seconds": 7200.0}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "[rate_limit] waiting for limit reset — retry 2 in 2h 0m" in out


def test_short_blind_retry_keeps_plain_line() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 3,
                     "backoff_seconds": 60.0}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "[rate_limit] retry 3 (60.0s)" in out
    assert "waiting for limit reset" not in out


def test_hinted_retry_with_missing_backoff_falls_through_to_plain_line() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 2, "hinted": True}},
        # backoff_seconds absent
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "waiting for limit reset" not in out
    assert "[rate_limit] retry 2" in out


def test_retry_breadcrumb_appends_the_provider_message() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "auth", "attempt": 2, "backoff_seconds": 2.0,
                     "message": "Failed to authenticate. API Error: 401"}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=120))

    assert ("warning: [auth] retry 2 (2.0s) — "
            "Failed to authenticate. API Error: 401") in out


def test_long_retry_message_is_cut_to_159_plus_an_ellipsis() -> None:
    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "auth", "attempt": 1, "backoff_seconds": 1.0,
                     "message": "x" * 200}},
    ])
    node = next(c for c in tree.root.children if isinstance(c, nv.ErrorNode))
    rendered_message = node.message.split(" — ", 1)[1]
    assert rendered_message == "x" * 159 + "…"
    assert len(rendered_message) == 160


def test_retry_breadcrumb_without_a_message_is_unchanged() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "stalled", "attempt": 2, "backoff_seconds": 4.0}},
        {"id": "e2", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "network", "attempt": 2, "backoff_seconds": 2.0}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=120))

    assert "warning: resuming — verifying prior work, not redoing it" in out
    assert "warning: [network] retry 2 (2.0s)" in out
    assert "—" not in out.split("[network]", 1)[1]


def test_hinted_retry_breadcrumb_ignores_the_message() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None,
         "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 2, "backoff_seconds": 1800.0,
                     "hinted": True, "message": "claude CLI: usage limit reached"}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=120))

    assert "[rate_limit] waiting for limit reset — retry 2 in 30m" in out
    assert "claude CLI: usage limit reached" not in out


def _sub_retry_events(**payload: Any) -> list[dict[str, Any]]:
    """A step that dispatches a `code-reviewer` subagent which then hits a retry."""
    return [
        {"id": "s1", "parent_id": None, "agent_id": None, "type": "step_started",
         "payload": {"step": "code-review", "kind": "agent", "phase": "convergence"}},
        {"id": "tk1", "parent_id": None, "agent_id": "step:convergence/code-review",
         "type": "tool_call",
         "payload": {"id": "tk1", "name": "sub_agent",
                     "input": {"subagent_name": "code-reviewer",
                               "task_description": "review the diff"}}},
        {"id": "r1", "parent_id": None, "agent_id": "sub:tk1", "type": "agent_retry",
         "payload": payload},
    ]


def _lean(events: list[dict[str, Any]]) -> str:
    from imp.cli.render_text import render_tree
    return "\n".join(render_tree(nv.build_tree(events), full=False, color=False, width=120))


def _full(events: list[dict[str, Any]]) -> str:
    from imp.cli.render_text import render_tree
    return "\n".join(render_tree(nv.build_tree(events), full=True, color=False, width=120))


PARENT_LINE = ("warning: [rate_limit] code-reviewer waiting for limit reset"
               " — retry 1 in 2h 45m")
IN_FOLD_LINE = "warning: [rate_limit] waiting for limit reset — retry 1 in 2h 45m"


def test_nested_subagent_limit_wait_leaves_one_outermost_breadcrumb() -> None:
    events = [
        {"id": "s1", "agent_id": None, "type": "step_started",
         "payload": {"step": "review", "kind": "agent", "phase": "convergence"}},
        {"id": "parent", "agent_id": "step:convergence/review", "type": "tool_call",
         "payload": {"id": "parent-block", "id_ns": "block", "name": "sub_agent",
                     "input": {"subagent_name": "parent"}}},
        {"id": "grandchild", "agent_id": "sub:parent", "type": "tool_call",
         "payload": {"id": "grandchild-block", "id_ns": "block", "name": "sub_agent",
                     "input": {"subagent_name": "grandchild"}}},
        {"id": "retry", "agent_id": "sub:grandchild", "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 1,
                     "backoff_seconds": 9908.0, "hinted": True}},
    ]

    named = ("warning: [rate_limit] grandchild waiting for limit reset"
             " — retry 1 in 2h 45m")
    assert _lean(events).count(named) == 1
    full = _full(events)
    assert full.count(named) == 1
    assert full.count(IN_FOLD_LINE) == 1


def test_subagent_limit_wait_leaves_a_parent_level_breadcrumb() -> None:
    # 9908s is the observed hinted wait: 165 min -> "2h 45m".
    events = _sub_retry_events(category="rate_limit", attempt=1,
                               backoff_seconds=9908.0, hinted=True)

    assert _lean(events).count(PARENT_LINE) == 1
    # lean hides the fold's contents, so the in-fold breadcrumb is NOT what shows.
    assert IN_FOLD_LINE not in _lean(events)

    # --full is unaffected: the in-fold line has no subagent name, so it is distinct
    full = _full(events)
    assert IN_FOLD_LINE in full
    assert full.count(PARENT_LINE) == 1


def test_subagent_limit_breadcrumb_stays_out_of_the_warnings_summary() -> None:
    events = _sub_retry_events(category="rate_limit", attempt=1,
                               backoff_seconds=9908.0, hinted=True)
    events.append({"id": "rw", "parent_id": None,
                   "agent_id": "step:convergence/code-review",
                   "type": "runner_warning", "payload": {"message": "an unrelated warning"}})
    events.append({"id": "w", "parent_id": None, "agent_id": None,
                   "type": "workflow_completed", "payload": {}})

    tree = nv.build_tree(events)
    summaries = [c for c in tree.root.children if isinstance(c, nv.WarningsSummaryNode)]
    # The unrelated warning proves the summary ran; the breadcrumb is absent from it.
    assert len(summaries) == 1
    assert summaries[0].lines == [("an unrelated warning", 1)]
    assert summaries[0].total == 1


def test_short_subagent_retry_leaves_no_parent_level_line() -> None:
    events = _sub_retry_events(category="rate_limit", attempt=2, backoff_seconds=30.0)

    assert "waiting for limit reset" not in _lean(events)
    assert "warning:" not in _lean(events)
    # the fold's own plain retry breadcrumb is untouched
    assert "warning: [rate_limit] retry 2 (30.0s)" in _full(events)


def test_stalled_subagent_retry_leaves_no_parent_level_line() -> None:
    # the only shape error_policy.decide produces for `stalled`: <=60s cap, no hint
    events = _sub_retry_events(category="stalled", attempt=2, backoff_seconds=60.0)

    lean = _lean(events)
    assert "code-reviewer" in lean   # the fold's status row is still there
    assert "warning:" not in lean
    assert "warning: resuming — verifying prior work, not redoing it" in _full(events)


ROTATED_2 = ("warning: [auth] retry 2 (1.0s) — another session rotated the "
             "OAuth token; retrying with the new one")


def _rotation_retry(attempt: int) -> list[dict[str, Any]]:
    return [{"id": "r1", "parent_id": None, "agent_id": None,
             "type": "agent_retry",
             "payload": {"category": "auth", "kind": "token_rotated",
                         "attempt": attempt, "backoff_seconds": 1.0,
                         "message": "claude CLI: Failed to authenticate. API "
                                    "Error: 401 OAuth access token has been "
                                    "revoked."}}]


def _lines(events: list[dict[str, Any]], *, full: bool) -> list[str]:
    from imp.cli.render_text import render_tree

    return [line.strip() for line in
            render_tree(nv.build_tree(events), full=full, color=False, width=160)]


def test_rotation_race_retry_renders_its_own_copy() -> None:
    lines = _lines(_rotation_retry(2), full=False)

    assert ROTATED_2 in lines
    # The provider's raw prose is replaced, not appended.
    assert not any("OAuth access token has been revoked" in ln for ln in lines)


def test_rotation_race_retry_stays_quiet_on_attempt_one() -> None:
    assert not any("rotated the OAuth token" in ln
                   for ln in _lines(_rotation_retry(1), full=False))
    assert any("[auth] retry 1 (1.0s) — another session rotated the OAuth token;"
               " retrying with the new one" in ln
               for ln in _lines(_rotation_retry(1), full=True))


def test_ordinary_auth_retry_copy_is_unchanged() -> None:
    events = [{"id": "r1", "parent_id": None, "agent_id": None,
               "type": "agent_retry",
               "payload": {"category": "auth", "kind": "http_401",
                           "attempt": 2, "backoff_seconds": 2.0,
                           "message": "Failed to authenticate. API Error: 401 "
                                      "OAuth access token has been revoked."}}]

    assert ("warning: [auth] retry 2 (2.0s) — Failed to authenticate. API Error: "
            "401 OAuth access token has been revoked.") in _lines(events, full=False)


def test_fmt_hm_renders_a_day_form_at_and_above_48_hours() -> None:
    from imp.cli.narrative import _fmt_hm

    assert _fmt_hm(187200) == "2d 4h"      # 52h — the weekly-window countdown shape
    assert _fmt_hm(172800) == "2d 0h"      # exactly 48h: the day form starts here
    assert _fmt_hm(172799) == "47h 59m"    # one second below: byte-unchanged
    assert _fmt_hm(691200) == "8d 0h"      # the 8-day ceiling a reset hint may carry


def test_the_retry_line_renders_a_multi_day_wait_in_days() -> None:
    from imp.cli.render_text import render_tree

    tree = nv.build_tree([
        {"id": "e1", "parent_id": None, "agent_id": None, "type": "agent_retry",
         "payload": {"category": "rate_limit", "attempt": 1,
                     "backoff_seconds": 187200.0, "hinted": True}},
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "[rate_limit] waiting for limit reset — retry 1 in 2d 4h" in out


def test_the_subagent_retry_mirror_renders_a_multi_day_wait_in_days() -> None:
    events = _sub_retry_events(category="rate_limit", attempt=1,
                               backoff_seconds=187200.0, hinted=True)

    assert ("warning: [rate_limit] code-reviewer waiting for limit reset"
            " — retry 1 in 2d 4h") in _lean(events)
