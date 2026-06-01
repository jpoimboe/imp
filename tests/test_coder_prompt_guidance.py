"""Implementer prompt explains the virtual /project namespace + bash cwd.

coder.md was replaced by implementer.md in the superpowers workflow port.
The virtual-path guidance (originally Fix C for coder.md) must be present
in implementer.md because it uses the bash tool and the same path semantics
apply.
"""

from __future__ import annotations

from pathlib import Path

IMPLEMENTER = (
    Path(__file__).resolve().parents[1]
    / "src" / "imp" / "assets" / "workflows" / "default" / "agents" / "implementer.md"
)


def test_implementer_prompt_mentions_project_root_env() -> None:
    body = IMPLEMENTER.read_text()
    assert "$IMP_PROJECT_ROOT" in body


def test_implementer_prompt_warns_against_literal_project_in_bash() -> None:
    body = IMPLEMENTER.read_text()
    low = body.lower()
    assert "virtual" in low
    assert "/project" in body
    # Explicitly steers away from `cd /project`.
    assert "cd /project" in body
