"""Fix A: agent_retry renders inline (self-healed retry visibility)."""

from __future__ import annotations

import json

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
         "payload": {"category": "rate_limit", "attempt": 1, "hinted": True}},
        # backoff_seconds absent
    ])
    out = "\n".join(render_tree(tree, full=False, color=False, width=80))

    assert "waiting for limit reset" not in out
    assert "[rate_limit] retry 1" in out
