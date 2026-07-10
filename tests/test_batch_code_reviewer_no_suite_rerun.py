from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_batch_code_reviewer_no_redundant_suite_rerun() -> None:
    body = (WF / "agents" / "batch-code-reviewer.md").read_text()
    # The full-suite re-run invitation is gone.
    assert "spot-check" not in body
    assert "you may re-run" not in body
    # The read-not-rerun directive and narrow escape hatch are present.
    assert "nothing new" in body
    assert "not re-run them" in body
    assert "narrowest check" in body
