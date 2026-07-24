from pathlib import Path

WF = Path("src/imp/assets/workflows/default")


def test_gap_check_carries_read_and_reason_no_suite_guard() -> None:
    body = (WF / "agents" / "gap-check.md").read_text()
    # (a) it must forbid running the project's full test suite, lint, or typecheck,
    #     attributing that to the phase-level verify gate(s).
    assert "full test suite, lint, or typecheck" in body
    assert "phase-level verify gate" in body
    # read-and-reason framing: glob/grep to spot-check, don't run the suite.
    assert "read-and-reason" in body
    # (b) the narrowest-single-check escape hatch is preserved.
    assert "single narrowest check that tests it" in body
    # Attribution must be FUTURE/ownership tense (converge-verify runs LATER in the
    # same phase), never a false past-tense "already ran".
    assert "later in this phase" in body
    assert "already ran" not in body
