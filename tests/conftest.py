"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
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
    if "test_mid_step_resume_live" in str(request.node.fspath):
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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main` and `.imp/` gitignored."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _seed_control(root: Path) -> None:
    """A git repo with committed imp/config.yaml + imp/workflows/default."""
    _init_repo(root)
    src = root / "imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "stub.md",
        frontmatter=("name: stub\ntools: [complete_step]\n"
                     "output:\n  status: {type: enum, values: [Success, Error]}"),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n  - name: p\n    steps:\n      - {name: s, file: agents/stub.md}\n"
    )
    (root / "imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low: {anthropic: x}\n"
        "  medium: {anthropic: x}\n"
        "  high: {anthropic: x}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def write_agent_md(path: Path, frontmatter: str, body: str = "prompt body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n")
    return path


@pytest.fixture
def provider_registry():
    """Reset the provider registry before AND after the test, yielding a thin
    register(...) wrapper over resolve.register_provider so tests keep their
    bespoke factories while the fixture owns teardown.

    Opt-in: name `provider_registry` as a test argument. Mirrors the
    reset-on-both-sides shape of test_subagent_spawn.py's autouse `_providers`
    fixture, but NON-autouse so only requesting tests are affected. The
    before-yield reset means a converted test no longer detects a registry
    polluted by a prior test (intentional parity with test_subagent_spawn.py);
    the after-yield reset still prevents leakage onto the next test.
    """
    from imp.providers import resolve

    resolve.reset_providers_for_tests()

    def register(name, env_check, factory):
        resolve.register_provider(name, env_check=env_check, factory=factory)

    yield register
    resolve.reset_providers_for_tests()
