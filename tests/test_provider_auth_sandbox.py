from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from imp.agents.tools.context import ToolContext
from imp.engine import worktree as wt
from imp.engine.env import ensure_provider_auth_dir, provider_auth_dir
from imp.engine.sandbox import (
    SandboxConfig,
    SandboxPolicy,
    resolve_policy,
    sandbox_available,
    with_scratch_slot,
)
from imp.paths import project_state_dir
from imp.providers.provider_auth import stage_provider_auth
from tests.conftest import _init_repo
from tests.test_sandbox_behavioral import _bash, _ctx

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable")

KINDS = ["plain", "slot", "isolated", "second-quest"]
PASSTHROUGH = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")


@pytest.fixture(autouse=True)
def _ambient_provider_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Different ambient values, also passed through: the policy env must still win.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/nonexistent/ambient-claude")
    monkeypatch.setenv("CODEX_HOME", "/nonexistent/ambient-codex")


@pytest.fixture
def control() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="imp-test-pauth-"))
    _init_repo(root)
    (root / "home").mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _cfg(control: Path) -> SandboxConfig:
    return SandboxConfig(home=control / "home", uid=os.getuid(), env_passthrough=PASSTHROUGH)


def _quest_policy(control: Path, quest_id: str) -> tuple[Path, SandboxPolicy]:
    worktree = wt.create_worktree(control, quest_id, "main")
    quest_dir = project_state_dir(control) / "quests" / quest_id
    quest_dir.mkdir(parents=True)
    return worktree, resolve_policy(
        worktree=worktree, control_root=control, quest_id=quest_id, cfg=_cfg(control),
        engine_env_result={"IMP_PROJECT_ROOT": str(worktree)}, environ=os.environ,
        available=True, quest_dir=quest_dir)


def _isolated_policy(
    control: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, SandboxPolicy]:
    import imp.agents.subagents as subagents

    monkeypatch.setattr("imp.engine.sandbox.sandbox_config_from_raw",
                        lambda raw: _cfg(control))
    monkeypatch.setattr("imp.engine.sandbox.overlay_mode", lambda _b, _h: None)
    monkeypatch.setattr(
        "imp.providers.effective.load_effective_config",
        lambda **kw: SimpleNamespace(config=SimpleNamespace(sandbox={}, pythonpath=())))
    quest_dir = project_state_dir(control) / "quests" / "pa1"
    quest_dir.mkdir(parents=True)
    spike = wt.create_worktree(control, "pa1-spike", "main")
    quest = SimpleNamespace(id="pa1", title="T", project_root=control,
                            control_root=control, quest_dir=quest_dir, base_branch="main")
    policy = subagents._build_isolated_policy(quest, spike, test_gates=None)
    policy, _cleanup = subagents._attach_isolated_home(policy, spike, quest_dir)
    return spike, policy


def _policy_for(
    kind: str, control: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, SandboxPolicy]:
    if kind == "isolated":
        return _isolated_policy(control, monkeypatch)
    if kind == "second-quest":
        _quest_policy(control, "pa1")
        return _quest_policy(control, "pb2")
    worktree, policy = _quest_policy(control, "pa1")
    return worktree, (with_scratch_slot(policy, "explorer-1") if kind == "slot" else policy)


def _stage_copy(tmp_path: Path) -> Path:
    claude, codex = tmp_path / "canon-claude", tmp_path / "canon-codex"
    claude.mkdir()
    codex.mkdir()
    (claude / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "STAGED_CLAUDE_AT", "refreshToken": "rt"}}))
    (codex / "auth.json").write_text(json.dumps(
        {"auth_mode": "chatgpt",
         "tokens": {"access_token": "STAGED_CODEX_AT", "refresh_token": "rt"}}))
    target = provider_auth_dir()
    ensure_provider_auth_dir(target)
    stage_provider_auth(target, env={"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)})
    return target


def _write_secrets(secrets: dict[Path, str]) -> None:
    for path, secret in secrets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret)


def _assert_masked(
    ctx: ToolContext, policy: SandboxPolicy, secrets: dict[Path, str],
) -> None:
    for path, secret in secrets.items():
        out = _bash(ctx, f"cat {shlex.quote(str(path))}")
        assert out["returncode"] != 0, (path, out)
        assert secret not in str(out["stdout"]), (path, out)
        assert not policy.allows_read(path), path


@pytest.mark.parametrize("kind", KINDS)
def test_every_sandbox_reads_the_staged_copy_but_cannot_write_it(
    kind: str, control: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _stage_copy(tmp_path)
    lock = target.with_name("provider-auth.lock")
    assert lock.is_file()
    worktree, policy = _policy_for(kind, control, monkeypatch)
    ctx = _ctx(worktree, policy)

    env = _bash(ctx, 'printf "%s|%s" "$CLAUDE_CONFIG_DIR" "$CODEX_HOME"')
    assert env["stdout"] == f"{target / 'claude'}|{target / 'codex'}", env
    cat = _bash(ctx, 'cat "$CLAUDE_CONFIG_DIR/.credentials.json" "$CODEX_HOME/auth.json"')
    assert cat["returncode"] == 0, cat
    assert "STAGED_CLAUDE_AT" in str(cat["stdout"]), cat
    assert "STAGED_CODEX_AT" in str(cat["stdout"]), cat
    for cmd in ('touch "$CLAUDE_CONFIG_DIR/new"', 'touch "$CODEX_HOME/new"',
                ': > "$CLAUDE_CONFIG_DIR/.credentials.json"', ': > "$CODEX_HOME/auth.json"'):
        out = _bash(ctx, cmd)
        assert out["returncode"] != 0, (cmd, out)
    scratch = _bash(ctx, 'f="$TMPDIR/pa-probe" && : > "$f" && rm "$f"')
    assert scratch["returncode"] == 0, scratch
    seen = _bash(ctx, f"[ -e {shlex.quote(str(lock))} ] && echo visible || echo hidden")
    assert str(seen["stdout"]).strip() == "hidden", seen

    assert sorted(p.name for p in (target / "claude").iterdir()) == [".credentials.json"]
    assert sorted(p.name for p in (target / "codex").iterdir()) == ["auth.json"]
    assert "STAGED_CLAUDE_AT" in (target / "claude" / ".credentials.json").read_text()
    staged = target / "claude" / ".credentials.json"
    assert policy.allows_read(staged)
    assert not policy.allows_write(staged)
    assert not policy.allows_read(lock)


@pytest.mark.parametrize("kind", KINDS)
def test_a_spawn_mints_a_missing_copy_dir(
    kind: str, control: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = provider_auth_dir()
    worktree, policy = _policy_for(kind, control, monkeypatch)
    assert not target.exists()

    out = _bash(_ctx(worktree, policy), 'ls -A "$CLAUDE_CONFIG_DIR" "$CODEX_HOME"')

    assert out["returncode"] == 0, out
    for d in (target, target / "claude", target / "codex"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_home_credential_stores_are_masked_but_claude_json_stays_readable(
    control: Path,
) -> None:
    home = control / "home"
    secrets = {
        home / ".claude" / ".credentials.json": "CLAUDE_RT_SECRET",
        home / ".claude" / "projects" / "x.jsonl": "TRANSCRIPT_SECRET",
        home / ".claude" / "settings.json": "SETTINGS_SECRET",
        home / ".codex" / "auth.json": "CODEX_RT_SECRET",
    }
    _write_secrets(secrets)
    (home / ".claude.json").write_text("PROJECT_LIST")
    worktree, policy = _quest_policy(control, "pa1")
    ctx = _ctx(worktree, policy)

    _assert_masked(ctx, policy, secrets)
    for store in (home / ".claude", home / ".codex"):
        listing = _bash(ctx, f"ls -A {shlex.quote(str(store))}")
        assert listing["returncode"] == 0, listing
        assert str(listing["stdout"]).strip() == "", listing
    kept = _bash(ctx, f"cat {shlex.quote(str(home / '.claude.json'))}")
    assert str(kept["stdout"]).strip() == "PROJECT_LIST", kept


def test_env_relocated_credential_stores_are_masked(
    control: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    alt_claude, alt_codex = tmp_path / "alt-claude", tmp_path / "alt-codex"
    secrets = {
        alt_claude / ".credentials.json": "ALT_CLAUDE_RT_SECRET",
        alt_claude / "projects" / "x.jsonl": "ALT_TRANSCRIPT_SECRET",
        alt_claude / "settings.json": "ALT_SETTINGS_SECRET",
        alt_codex / "auth.json": "ALT_CODEX_RT_SECRET",
    }
    _write_secrets(secrets)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt_claude))
    monkeypatch.setenv("CODEX_HOME", str(alt_codex))
    worktree, policy = _quest_policy(control, "pa1")

    _assert_masked(_ctx(worktree, policy), policy, secrets)
