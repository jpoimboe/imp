"""Startup model_role resolution: fail-loud if any role can't be resolved."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.agents.stub_runner import StubAgentRunner
from imp.engine.change import Change
from imp.engine.executor import run
from imp.providers.resolve import ProviderUnavailable

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path: Path) -> Change:
    shutil.copytree(
        FIXTURES / "workflows" / "integration",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp_path, title="t")


def test_run_resolves_at_startup_raises_when_env_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(
        "IMP_STUB_CANNED", str(FIXTURES / "canned" / "stub_outputs.yaml")
    )
    monkeypatch.chdir(tmp_path)
    change = _seed(tmp_path)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    with pytest.raises(ProviderUnavailable):
        run(change, runner)


def test_run_succeeds_with_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Autouse fixture sets ANTHROPIC_API_KEY; nothing to do.
    monkeypatch.setenv(
        "IMP_STUB_CANNED", str(FIXTURES / "canned" / "stub_outputs.yaml")
    )
    monkeypatch.chdir(tmp_path)
    change = _seed(tmp_path)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    result = run(change, runner)
    assert result.value in ("completed", "paused")
