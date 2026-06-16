from __future__ import annotations

from imp.assets import bundled_workflow_dir


def _finalize_body() -> str:
    return (bundled_workflow_dir("imp") / "agents" / "finalize.md").read_text(
        encoding="utf-8")


def test_finalize_declares_a_before_after_capture_section() -> None:
    body = _finalize_body()
    assert "Before/after" in body
    # Trigger is what the change touched (git diff), not a playbook.
    assert "git diff --name-only" in body
    assert "src/imp/cli/" in body
    assert "src/impty/" in body


def test_finalize_gates_capture_on_the_impty_driver_and_presents_at_the_gate() -> None:
    body = _finalize_body()
    assert "mcp__impty__snapshot" in body
    assert "mcp__impty__start" in body
    # Both frames, presented at the merge-gate wait_for_user.
    assert "**before**" in body and "**after**" in body
    assert "PNG paths" in body
