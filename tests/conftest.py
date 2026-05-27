"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _stub_provider_api_keys(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub provider env vars only if not already set. Live-API tests get
    the real env through (their own skipif handles the "no auth" case);
    ordinary tests get the stub so module-level provider construction works.

    The Claude Code live test spawns `claude -p` as a subprocess, which falls
    back to user subscription auth when ANTHROPIC_API_KEY is unset. Stubbing
    it to "sk-test" makes the subprocess try the fake key and fail with
    "Invalid API key". So we skip stubbing for any test file whose name
    matches the live-API pattern.
    """
    if "test_default_workflow_e2e_live" in str(request.node.fspath):
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    if not os.environ.get("OPENAI_API_KEY"):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """An empty 'project' directory under tmp_path."""
    (tmp_path / ".imp").mkdir()
    return tmp_path


def write_agent_md(path: Path, frontmatter: str, body: str = "prompt body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return path
