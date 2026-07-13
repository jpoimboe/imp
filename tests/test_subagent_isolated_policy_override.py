from __future__ import annotations

from types import SimpleNamespace

import imp.agents.subagents as subagents
from imp.engine.sandbox import SandboxPolicy


def _policy(*, active: bool, note: str = "") -> SandboxPolicy:
    return SandboxPolicy(
        active=active, worktree="/wt", git_common_dir="/wt/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(),
        extra_writable=(), secret_dirs=(), secret_files=(), setenv=(),
        network=True, note=note)


def _hermetic(monkeypatch, resolved: SandboxPolicy) -> None:
    # _build_isolated_policy imports these names locally at call time, so patch
    # them on their defining modules. resolve_policy ignores its kwargs here, so
    # the env/config machinery just needs to not raise.
    monkeypatch.setattr("imp.engine.sandbox.resolve_policy", lambda **k: resolved)
    monkeypatch.setattr("imp.engine.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr("imp.engine.sandbox.sandbox_config_from_raw",
                        lambda raw: object())
    monkeypatch.setattr("imp.engine.env.engine_env", lambda **k: {})
    monkeypatch.setattr("imp.engine.env.overlay_engine_env", lambda *a, **k: {})
    monkeypatch.setattr("imp.providers.effective.load_effective_config",
                        lambda **k: SimpleNamespace(
                            config=SimpleNamespace(sandbox={}, pythonpath=())))


def _quest() -> SimpleNamespace:
    return SimpleNamespace(project_root="/wt", control_root="/c", id="q",
                           title="t", quest_dir="/c/.imp/quests/q")


def test_isolated_policy_forced_inactive_when_parent_unsandboxed(monkeypatch) -> None:
    from pathlib import Path
    _hermetic(monkeypatch, _policy(active=True))
    parent = _policy(active=False, note="custom_session_note")

    out = subagents._build_isolated_policy(
        _quest(), Path("/spike"), None, parent_policy=parent)

    assert out.active is False
    assert out.note == "custom_session_note"


def test_isolated_policy_stays_active_when_parent_sandboxed(monkeypatch) -> None:
    from pathlib import Path
    _hermetic(monkeypatch, _policy(active=True))
    parent = _policy(active=True)

    out = subagents._build_isolated_policy(
        _quest(), Path("/spike"), None, parent_policy=parent)

    assert out.active is True
