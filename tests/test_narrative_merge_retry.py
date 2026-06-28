from imp.cli.narrative import NarrativeBuilder
from imp.cli.render_text import render_tree


def test_merge_retry_renders_attempt_line() -> None:
    b = NarrativeBuilder()
    b.add({"type": "merge_retry", "agent_id": None,
           "payload": {"status": "CONFLICT", "attempt": 2, "max": 3, "base": "main"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "retrying integration (attempt 2/3)" in out
    assert "⚠" not in out                       # not the merge_blocked block


def test_merge_retry_denominator_comes_from_payload() -> None:
    b = NarrativeBuilder()
    b.add({"type": "merge_retry", "agent_id": None,
           "payload": {"status": "DIRTY", "attempt": 1, "max": 5, "base": "main"}})
    out = "\n".join(render_tree(b.tree, full=False, color=False, width=80))
    assert "retrying integration (attempt 1/5)" in out  # denominator from payload, not hardcoded
