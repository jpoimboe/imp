"""Inventory guard: the review-eval harness never runs the spec phase."""
from __future__ import annotations


def test_evals_do_not_run_spec_phase() -> None:
    # Inventory assertion (design Headless rollout): the review-eval harness
    # replays review steps only and never runs the spec phase. Guard the claim by
    # reading the eval driver source directly (no import resolution needed) so a
    # future eval that adds a spec step trips this test and forces a re-inventory.
    from pathlib import Path

    driver = Path("src/imp/evals/driver.py").read_text()
    assert "spec-writer" not in driver and "spec-reviewer" not in driver, (
        "the eval driver now references a spec step — re-inventory the headless "
        "rollout review path")
