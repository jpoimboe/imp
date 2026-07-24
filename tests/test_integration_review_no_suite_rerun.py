from pathlib import Path

WF = Path("src/imp/assets/workflows/default")


def test_integration_review_no_redundant_suite_rerun() -> None:
    body = (WF / "agents" / "integration-review.md").read_text()
    # The full-suite spot-check invitation is gone.
    assert "spot-check" not in body
    # The structural-guarantee wording and narrow escape hatch are present.
    assert "nothing new" in body
    assert "narrowest check" in body
    # The reasoning anchor is preserved.
    assert "base-side incoming diff" in body
    # The guard now says the gate already RAN immediately before the review —
    # robust regardless of which entry path the agent arrived through.
    assert "ran immediately before this review" in body
    # The strict "passed at the current tree" phrasing (false for the triage entry)
    # is gone from the Judge section.
    assert "already passed at the current tree" not in body
