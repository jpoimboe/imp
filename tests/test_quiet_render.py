from __future__ import annotations

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree
from tests._event_fixtures import ev


def _body(events, *, color=False) -> list[str]:
    return list(render_tree(nv.build_tree(events), full=False, color=color, width=80))


def test_quiet_continue_renders_nothing() -> None:
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="commit-check", kind="script",
           phase="implement", quiet=True),
        ev("3", "step_completed", step="commit-check", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ])
    # No step header, no completion breadcrumb, no raw goto — nothing at all.
    assert not any("commit-check" in ln for ln in body)


def test_quiet_resume_renders_only_the_rework_arrow() -> None:
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-verify-commit", kind="script",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="spec-verify-commit", kind="script",
           action={"verb": "goto", "target": "spec-writer", "resume": True},
           var_mutations={}),
    ])
    assert any(ln.strip() == "→ reworking" for ln in body)
    assert not any("spec-verify-commit" in ln for ln in body)
    assert not any("goto(" in ln for ln in body)


def test_quiet_forward_goto_renders_only_the_phase_arrow() -> None:
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="fast-path-spec-gate", kind="branch",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="fast-path-spec-gate", kind="branch",
           action={"verb": "goto", "target": "plan-writer", "resume": False},
           var_mutations={}),
        ev("4", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
    ])
    assert any(ln.strip() == "→ plan" for ln in body)
    assert not any("fast-path-spec-gate" in ln for ln in body)


def test_quiet_pause_yields_to_full_block() -> None:
    # Spec §4.1: a quiet step "yields to any non-linear outcome". A pause
    # (merge_blocked / retry_ceiling / max-retries — executor emits
    # step_completed with verb="pause") must NOT be swallowed: the step renders
    # its full block so the failure and its error children stay visible.
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="merge-to-base", kind="script",
           phase="merge", quiet=True),
        ev("3", "error", message="merge conflict in foo.py"),
        ev("4", "step_completed", step="merge-to-base", kind="script",
           action={"verb": "pause"}, var_mutations={}),
    ])
    assert any("merge-to-base" in ln for ln in body)               # header surfaced
    assert any("merge conflict in foo.py" in ln for ln in body)    # error not swallowed


def test_quiet_same_phase_goto_renders_nothing() -> None:
    # panel-gate → recheck (both in review): a same-phase skip renders NOTHING —
    # neither a loud block nor a redundant "→ review" arrow.
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="panel-gate", kind="branch",
           phase="review", quiet=True),
        ev("3", "step_completed", step="panel-gate", kind="branch",
           action={"verb": "goto", "target": "recheck", "resume": False},
           var_mutations={}),
        ev("4", "step_started", step="recheck", kind="agent", phase="review",
           model="m", provider="anthropic"),
    ])
    assert not any("panel-gate" in ln for ln in body)
    assert not any("→ review" in ln for ln in body)
    assert not any("goto(" in ln for ln in body)


def test_quiet_arrow_is_dim_gray_chrome_in_color() -> None:
    body = _body([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-verify-commit", kind="script",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="spec-verify-commit", kind="script",
           action={"verb": "goto", "target": "spec-writer", "resume": True},
           var_mutations={}),
    ], color=True)
    # breadcrumb/chrome gray (NODE_COLORS["breadcrumb"] == ("gray",)) → \x1b[90m.
    assert any("\x1b[90m→ reworking\x1b[0m" in ln for ln in body)
