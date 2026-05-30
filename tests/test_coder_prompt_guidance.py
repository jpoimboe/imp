"""Fix C: coder prompt explains the virtual /project namespace + bash cwd."""

from __future__ import annotations

from pathlib import Path

CODER = (
    Path(__file__).resolve().parents[1]
    / "src" / "imp" / "assets" / "workflows" / "default" / "agents" / "coder.md"
)


def test_coder_prompt_mentions_project_root_env() -> None:
    body = CODER.read_text()
    assert "$IMP_PROJECT_ROOT" in body


def test_coder_prompt_warns_against_literal_project_in_bash() -> None:
    body = CODER.read_text()
    low = body.lower()
    assert "virtual" in low
    assert "/project" in body
    # Explicitly steers away from `cd /project`.
    assert "cd /project" in body
