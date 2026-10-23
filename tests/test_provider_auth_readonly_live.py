"""Live: one-step claude and codex runs authenticate from a read-only credentials copy
built by the sandbox staging code, with every refresh token removed.

Fails rather than skips on a dead host login, so a green suite cannot hide one.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from imp.engine.env import PROVIDER_AUTH_ENV, ensure_provider_auth_dir
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.providers import claude_token_refresh, codex_token_refresh
from imp.providers.provider_auth import stage_provider_auth
from tests.conftest import _git

_ENV_VAR = {sub: var for var, sub in PROVIDER_AUTH_ENV}
_CREDS = {"claude": ".credentials.json", "codex": "auth.json"}
_REFRESH = {"claude": ("claudeAiOauth", "refreshToken"), "codex": ("tokens", "refresh_token")}
_MODULES = {"claude": claude_token_refresh, "codex": codex_token_refresh}
_MODELS = {"claude": "claude-haiku-4-5-20251001", "codex": "gpt-5.6-luna"}
_LOGIN = {"claude": "run `claude` and /login", "codex": "run `codex login`"}
_MIN_TOKEN_LIFE_MS = 5 * 60 * 1000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _ensure_builtin_providers() -> None:
    # The provider registry is import-time global state that other tests clear.
    from imp.providers import register_builtin_providers
    register_builtin_providers()


def _refresh_token_digest(provider: str, path: Path) -> str | None:
    # A digest, so a failing comparison never prints a live refresh token.
    if not path.is_file():
        return None
    section, key = _REFRESH[provider]
    token = (json.loads(path.read_text()).get(section) or {}).get(key)
    return hashlib.sha256(token.encode()).hexdigest() if token else None


def _snapshot(root: Path) -> dict[str, str]:
    # Contents, not just names: as namespace root, chmod alone may not stop a rewrite.
    return {str(p.relative_to(root)): "dir" if p.is_dir()
            else hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")}


def _set_read_only(root: Path, *, read_only: bool) -> None:
    subdirs = [root / sub for _, sub in PROVIDER_AUTH_ENV]
    for d in subdirs:
        for f in d.iterdir():
            f.chmod(0o400 if read_only else 0o600)
    for d in (root, *subdirs):
        d.chmod(0o500 if read_only else 0o700)


def _project(root: Path, provider: str) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n"
        "Call complete_step with status=Success. Do not call any other tool.\n"
    )
    (wf / "workflow.yaml").write_text(
        f"name: live-ro-{provider}\ndescription: One-step test\n"
        "defaults: {max_retries: 2, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    model = _MODELS[provider]
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\n"
        f"providers: [{provider}]\n"
        "tiers:\n"
        f"  low:    {{{provider}: {model}}}\n"
        f"  medium: {{{provider}: {model}}}\n"
        f"  high:   {{{provider}: {model}}}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


@pytest.mark.parametrize("provider", [
    pytest.param("claude", marks=pytest.mark.live("claude")),
    pytest.param("codex", marks=pytest.mark.live("codex")),
])
@pytest.mark.anyio
async def test_one_step_run_authenticates_from_the_read_only_copy(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the copy may authenticate: no API key or env OAuth token reaches either CLI.
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    module = _MODULES[provider]
    canonical = module.canonical_dir(os.environ) / _CREDS[provider]
    canonical_refresh = _refresh_token_digest(provider, canonical)

    copy = tmp_path / "provider-auth"
    ensure_provider_auth_dir(copy)
    stage_provider_auth(copy, env=os.environ)
    staged = copy / provider / _CREDS[provider]
    assert staged.is_file(), (
        f"no {provider} OAuth login to stage from {canonical}; {_LOGIN[provider]}")
    assert _refresh_token_digest(provider, staged) is None, (
        f"staged {provider} {staged.name} carries a refresh token")
    if provider == "codex":
        assert json.loads(staged.read_text())["tokens"]["refresh_token"] == ""
    expires_at = module.canonical_expires_at(env={_ENV_VAR[provider]: str(staged.parent)})
    if expires_at is not None and expires_at - int(time.time() * 1000) < _MIN_TOKEN_LIFE_MS:
        pytest.fail(f"host {provider} access token expires within 5 minutes; refresh the "
                    "host login and rerun")

    for var, sub in PROVIDER_AUTH_ENV:
        monkeypatch.setenv(var, str(copy / sub))
    before = _snapshot(copy)
    proj = tmp_path / "proj"
    proj.mkdir()
    _project(proj, provider)
    quest = Quest.create(
        proj, title=f"live-ro-{provider}", quest_id=f"live-ro-{provider}", base_branch="main")

    try:
        _set_read_only(copy, read_only=True)
        result = await run_quest(quest)
    finally:
        _set_read_only(copy, read_only=False)

    events = list(quest.event_log().read())
    warnings = [str((e.get("payload") or {}).get("message", ""))
                for e in events if e.get("type") == "runner_warning"]
    assert result is RunResult.COMPLETED, warnings
    copy_warnings = [w for w in warnings if "for sandboxed tools" in w]
    assert copy_warnings == []
    assert _snapshot(copy) == before                  # no file appeared or changed
    assert _refresh_token_digest(provider, canonical) == canonical_refresh
