"""The shared unattended-run config helper pins spec_confirm:false (single site)."""
from __future__ import annotations

from tests._unattended import render_vars_yaml, unattended_vars


def test_unattended_vars_pins_spec_confirm_false() -> None:
    assert unattended_vars()["spec_confirm"] is False


def test_unattended_vars_merges_extra_overrides() -> None:
    v = unattended_vars(fast_path=True)
    assert v == {"spec_confirm": False, "fast_path": True}


def test_render_vars_yaml_emits_a_vars_block() -> None:
    block = render_vars_yaml({"spec_confirm": False, "fast_path": True})
    assert block.startswith("vars:\n")
    assert "  spec_confirm: false\n" in block
    assert "  fast_path: true\n" in block


def test_evals_do_not_run_spec_phase() -> None:
    # Inventory assertion (design Headless rollout): the review-eval harness
    # replays review steps only and never runs the spec phase. Guard the claim by
    # reading the eval driver source directly (no import resolution needed) so a
    # future eval that adds a spec step trips this test and forces a re-inventory.
    from pathlib import Path

    driver = Path("src/imp/evals/driver.py").read_text()
    assert "spec-writer" not in driver and "spec-reviewer" not in driver, (
        "the eval driver now references a spec step — re-inventory the headless "
        "rollout: this entry point may need spec_confirm:false")
