from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def _started(eid: str, step: str, phase: str = "draft") -> dict:
    return {"id": eid, "agent_id": None, "type": "step_started",
            "payload": {"step": step, "kind": "agent", "phase": phase}}


def _completed(eid: str, step: str) -> dict:
    return {"id": eid, "agent_id": None, "type": "step_completed",
            "payload": {"step": step, "kind": "agent",
                        "action": {"verb": "goto", "target": "design-spec"},
                        "var_mutations": {}}}


def _transition(eid: str, frm: str, to: str, reason: str | None) -> dict:
    return {"id": eid, "agent_id": None, "type": "step_transition",
            "payload": {"from_step": frm, "target_step": to,
                        "backward": True, "reason": reason}}


def test_step_transition_renders_marker_with_reason() -> None:
    b = NarrativeBuilder()
    b.add(_transition("t", "claim-coverage-figures", "design-spec",
                      "Gate `check` failed (exit 1)."))
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=120))
    assert "↩ claim-coverage-figures → design-spec" in out
    assert "(Gate `check` failed (exit 1).)" in out


def test_step_transition_without_reason_omits_parens() -> None:
    b = NarrativeBuilder()
    b.add(_transition("t", "gate", "lead", None))
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=120))
    assert "↩ gate → lead" in out
    assert "(" not in out.split("↩ gate → lead", 1)[1].split("\n")[0]


def test_step_transition_renders_between_the_two_steps() -> None:
    b = NarrativeBuilder()
    for e in (
        _started("s1", "claim-coverage-figures"),
        _completed("c1", "claim-coverage-figures"),
        _transition("t", "claim-coverage-figures", "design-spec", "gate failed"),
        _started("s2", "design-spec"),
        _completed("c2", "design-spec"),
    ):
        b.add(e)
    lines = list(render_tree(b.tree, full=False, color=False, width=120))
    needle = "↩ claim-coverage-figures → design-spec"
    marker_i = next(i for i, ln in enumerate(lines) if needle in ln)
    # the source step's completion breadcrumb is above the marker ...
    src_i = next(i for i, ln in enumerate(lines)
                 if "claim-coverage-figures" in ln and "complete" in ln)
    # ... and the target step's own line is below it (not displaced to the bottom)
    tgt_i = next(i for i, ln in enumerate(lines)
                 if "design-spec" in ln and i > marker_i)
    assert src_i < marker_i < tgt_i
