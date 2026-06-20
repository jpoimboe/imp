from __future__ import annotations

from pathlib import Path

AGENTS = Path("src/imp/assets/workflows/imp/agents")
PROMPT = AGENTS / "spec-writer.md"


def test_final_readback_subsection_present_and_positioned() -> None:
    text = PROMPT.read_text()
    readback = text.find("**Final readback before committing.**")
    approve = text.find("**Design-then-approve.**")
    clarity = text.find("**Design for clarity.**")
    assert approve != -1 and readback != -1 and clarity != -1
    assert approve < readback < clarity


def test_final_readback_requires_one_consolidated_confirmation() -> None:
    text = PROMPT.read_text()
    start = text.find("**Final readback before committing.**")
    block = text[start : text.find("**Design for clarity.**")]
    low = block.lower()
    assert "consolidated" in low
    for token in ("goal", "scope", "decision", "acceptance"):
        assert token in low
    assert "wait_for_user" in block
    assert "before you write and commit" in low
    assert "not another round" in low


def test_final_readback_carves_out_all_four_cases() -> None:
    text = PROMPT.read_text()
    start = text.find("**Final readback before committing.**")
    block = text[start : text.find("**Design for clarity.**")].lower()
    assert "seeded" in block and "adopt-with-light-review" in block
    assert "trivial" in block
    assert "rework" in block and "minors" in block and "gate-discussion" in block
    assert "headless" in block


def test_closing_summary_guards_against_restating_readback() -> None:
    text = PROMPT.read_text()
    start = text.find("**One closing summary.**")
    block = text[start : text.find("## Seeded with a draft spec")].lower()
    assert "final readback" in block and "not" in block
