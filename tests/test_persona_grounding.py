from __future__ import annotations

from pathlib import Path

PERSONA = Path("src/imp/assets/workflows/imp/persona.md")


def _doing_tasks_first_bullet() -> str:
    text = PERSONA.read_text()
    start = text.index("# Doing tasks")
    # The first bullet runs from the "# Doing tasks" heading to the second bullet.
    rest = text[start:]
    second = rest.index("\n - You are highly capable")
    return rest[:second].lower()


def test_first_principle_grounds_agent_in_a_concrete_repo() -> None:
    bullet = _doing_tasks_first_bullet()
    assert "always operating inside a concrete repository" in bullet
    # Determine the stack by reading, not asking.
    assert "build system" in bullet
    assert "not by asking" in bullet
    # An established stack is known ground.
    assert "established stack" in bullet
    # Greenfield is preserved via "read first", not "assume".
    assert "read first" in bullet
    assert "greenfield" in bullet


def test_first_principle_does_not_assert_stack_always_known() -> None:
    bullet = _doing_tasks_first_bullet()
    # The carve-out must not be flattened into an absolute claim.
    assert "always known" not in bullet
    assert "stack is always" not in bullet
