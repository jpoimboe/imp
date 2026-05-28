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

    The dogfood live test and the e2e live tests spawn subprocesses that
    read ANTHROPIC_API_KEY from the real env. Stubbing it to "sk-test"
    would give the subprocess a fake key and cause 401 errors. Skip
    stubbing for test files whose name matches the live-API patterns.
    """
    if "test_default_workflow_e2e_live" in str(request.node.fspath):
        return
    if "test_default_workflow_dogfood_live" in str(request.node.fspath):
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
