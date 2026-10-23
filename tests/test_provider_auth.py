from __future__ import annotations

import base64
import json
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import filelock
import pytest

from imp.engine.env import ensure_provider_auth_dir, provider_auth_dir
from imp.providers import provider_auth as pa
from imp.providers.provider_auth import stage_provider_auth

NOW = 2_000_000_000_000            # ms; every call takes now_ms explicitly
HOUR_MS = 3_600_000
WINDOW = 3_900_000                 # freshness_window_ms(3600)
FAR = NOW + 2 * HOUR_MS            # outside WINDOW
NEAR = NOW + HOUR_MS // 2          # inside WINDOW, outside the refresh modules' 5-min buffer
STAGED = ("claude/.credentials.json", "claude/.claude.json", "codex/auth.json")
STAGE_WARNING = "couldn't update the credentials copy for sandboxed tools; retrying next step"


def _jwt(exp_s: int | None, tag: str) -> str:
    claims: dict[str, Any] = {"tag": tag}
    if exp_s is not None:
        claims["exp"] = exp_s
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _put(path: Path, body: Any) -> None:
    """Atomically replace `path` with compact, unsorted JSON (not the staged form)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, path)


@dataclass
class Canon:
    claude: Path
    codex: Path

    @property
    def env(self) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)}

    @property
    def claude_json(self) -> Path:
        return self.claude / ".claude.json"

    def write_claude(self, *, access: str = "claude-at", refresh: str = "claude-rt",
                     expires_at: int | None = FAR) -> None:
        oauth: dict[str, Any] = {"accessToken": access, "refreshToken": refresh,
                                 "scopes": ["user:inference"], "subscriptionType": "max"}
        if expires_at is not None:
            oauth["expiresAt"] = expires_at
        _put(self.claude / ".credentials.json", {"claudeAiOauth": oauth})

    def write_codex(self, *, access: str | None = None, refresh: str = "codex-rt",
                    auth_mode: str | None = "chatgpt") -> None:
        body: dict[str, Any] = {
            "OPENAI_API_KEY": None,
            "tokens": {"access_token": access or _jwt(FAR // 1000, "codex"),
                       "refresh_token": refresh, "id_token": "codex-id",
                       "account_id": "acct"},
            "last_refresh": "2033-05-18T00:00:00Z",
        }
        if auth_mode is not None:
            body["auth_mode"] = auth_mode
        _put(self.codex / "auth.json", body)


@pytest.fixture
def canon(tmp_path: Path) -> Canon:
    c = Canon(claude=tmp_path / "canon-claude", codex=tmp_path / "canon-codex")
    c.claude.mkdir()
    c.codex.mkdir()
    c.write_claude()
    c.claude_json.write_text('{"primaryApiKey": "sk-ant-x",\n "projects": {"/p": {}}}\n')
    c.write_codex()
    return c


@pytest.fixture
def target() -> Path:
    return provider_auth_dir()


def _staged(target: Path, rel: str) -> dict[str, Any]:
    return json.loads((target / rel).read_text())


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _mtimes(target: Path) -> dict[str, int]:
    return {str(p.relative_to(target)): p.stat().st_mtime_ns
            for p in sorted(target.rglob("*")) if p.is_file()}


def _freeze_mtimes(target: Path) -> dict[str, int]:
    """Backdate every staged file, so any rewrite shows whatever the clock granularity."""
    for p in target.rglob("*"):
        if p.is_file():
            os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    return _mtimes(target)


def _stage(target: Path, env: dict[str, str]) -> None:
    ensure_provider_auth_dir(target)
    stage_provider_auth(target, env=env)


def test_staging_projects_access_tokens_only_with_private_modes(
    canon: Canon, target: Path,
) -> None:
    for name in ("settings.json", "cowork_settings.json"):
        (canon.claude / name).write_text('{"apiKeyHelper": "echo sk"}')

    _stage(target, canon.env)

    assert target.parent.name == f"imp-{os.getuid()}"
    for d in (target, target / "claude", target / "codex"):
        assert _mode(d) == 0o700
    creds = _staged(target, "claude/.credentials.json")["claudeAiOauth"]
    assert creds["accessToken"] == "claude-at"
    assert "refreshToken" not in creds
    assert (target / "claude/.claude.json").read_bytes() == canon.claude_json.read_bytes()
    expected_codex = json.loads((canon.codex / "auth.json").read_text())
    expected_codex["tokens"]["refresh_token"] = ""
    assert _staged(target, "codex/auth.json") == expected_codex
    for rel in STAGED:
        assert _mode(target / rel) == 0o600
    assert sorted(p.name for p in (target / "claude").iterdir()) == [
        ".claude.json", ".credentials.json"]
    assert sorted(p.name for p in (target / "codex").iterdir()) == ["auth.json"]
    assert sorted(p.name for p in target.iterdir()) == ["claude", "codex"]
    assert (target.parent / "provider-auth.lock").is_file()


def test_a_stored_codex_api_key_is_nulled_in_the_staged_copy(
    canon: Canon, target: Path,
) -> None:
    body = json.loads((canon.codex / "auth.json").read_text())
    body["OPENAI_API_KEY"] = "sk-stored"
    _put(canon.codex / "auth.json", body)

    _stage(target, canon.env)

    staged = _staged(target, "codex/auth.json")
    assert "OPENAI_API_KEY" in staged
    assert staged["OPENAI_API_KEY"] is None


@pytest.mark.parametrize("case", ["no-file", "apikey", "implicit-apikey", "chatgpt-no-tokens"])
def test_codex_copy_is_staged_only_for_a_chatgpt_login_with_tokens(
    canon: Canon, target: Path, case: str,
) -> None:
    env = canon.env
    if case == "no-file":
        (canon.codex / "auth.json").unlink()
    elif case == "apikey":
        canon.write_codex(auth_mode="apikey")
    elif case == "implicit-apikey":
        canon.write_codex(auth_mode=None)
        env = {**env, "OPENAI_API_KEY": "sk-env"}
    else:
        _put(canon.codex / "auth.json", {"auth_mode": "chatgpt"})

    _stage(target, env)

    assert not (target / "codex/auth.json").exists()
    assert (target / "claude/.credentials.json").is_file()


def test_an_implicit_chatgpt_codex_login_is_staged(canon: Canon, target: Path) -> None:
    canon.write_codex(auth_mode=None)
    _stage(target, canon.env)
    assert _staged(target, "codex/auth.json")["tokens"]["refresh_token"] == ""


def test_api_key_user_gets_no_credentials_and_a_rerun_writes_nothing(
    canon: Canon, target: Path,
) -> None:
    (canon.claude / ".credentials.json").unlink()
    _stage(target, canon.env)
    assert not (target / "claude/.credentials.json").exists()
    assert (target / "claude/.claude.json").is_file()

    before = _freeze_mtimes(target)
    stage_provider_auth(target, env=canon.env)
    assert _mtimes(target) == before


def test_staging_sweeps_temp_files_orphaned_by_a_crash(canon: Canon, target: Path) -> None:
    _stage(target, canon.env)
    orphans = [target / "claude/.credentials.json.tmp-abc", target / "codex/auth.json.tmp-xyz"]
    for orphan in orphans:
        orphan.write_text("stale-access-token")

    stage_provider_auth(target, env=canon.env)

    for orphan in orphans:
        assert not orphan.exists()
    assert sorted(p.name for p in (target / "claude").iterdir()) == [
        ".claude.json", ".credentials.json"]


def test_a_logout_unstages_every_file(canon: Canon, target: Path) -> None:
    _stage(target, canon.env)
    for p in (canon.claude / ".credentials.json", canon.claude_json, canon.codex / "auth.json"):
        p.unlink()

    stage_provider_auth(target, env=canon.env)

    for rel in STAGED:
        assert not (target / rel).exists(), rel


@pytest.mark.parametrize("body", ["{not json", "[]", '{"claudeAiOauth": "x"}'])
def test_unusable_claude_credentials_are_not_staged(
    canon: Canon, target: Path, body: str,
) -> None:
    _stage(target, canon.env)
    (canon.claude / ".credentials.json").write_text(body)

    stage_provider_auth(target, env=canon.env)

    assert not (target / "claude/.credentials.json").exists()


def test_only_the_claude_oauth_entry_is_staged(canon: Canon, target: Path) -> None:
    _put(canon.claude / ".credentials.json", {
        "claudeAiOauth": {"accessToken": "claude-at", "refreshToken": "claude-rt",
                          "expiresAt": FAR},
        "mcpOAuth": {"srv": {"accessToken": "mcp-at", "refreshToken": "mcp-rt"}},
    })
    _stage(target, canon.env)
    assert _staged(target, "claude/.credentials.json") == {
        "claudeAiOauth": {"accessToken": "claude-at", "expiresAt": FAR}}


def test_staged_bytes_are_deterministic(canon: Canon, target: Path) -> None:
    (canon.claude / ".credentials.json").write_text(
        '{"claudeAiOauth": {"scopes": ["a"], "refreshToken": "r", "accessToken": "t"}}')
    _stage(target, canon.env)
    assert (target / "claude/.credentials.json").read_bytes() == json.dumps(
        {"claudeAiOauth": {"accessToken": "t", "scopes": ["a"]}},
        indent=2, sort_keys=True).encode("utf-8")

    # The same content in another key order is the same projection: no rewrite.
    before = _freeze_mtimes(target)
    (canon.claude / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "t", "refreshToken": "r", "scopes": ["a"]}}')
    body = json.loads((canon.codex / "auth.json").read_text())
    _put(canon.codex / "auth.json", dict(reversed(list(body.items()))))
    stage_provider_auth(target, env=canon.env)
    assert _mtimes(target) == before


def test_claude_credentials_fall_back_to_the_keychain_like_the_sdk(
    tmp_path: Path, target: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk._internal import session_resume

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    consulted: list[int] = []

    def _keychain() -> str:
        consulted.append(1)
        return json.dumps({"claudeAiOauth": {"accessToken": "kc-at", "refreshToken": "kc-rt"}})

    monkeypatch.setattr(session_resume, "_read_keychain_credentials", _keychain)

    _stage(target, {})
    assert consulted == [1]
    assert _staged(target, "claude/.credentials.json") == {
        "claudeAiOauth": {"accessToken": "kc-at"}}

    consulted.clear()
    stage_provider_auth(target, env={"ANTHROPIC_API_KEY": "sk-ant"})
    assert consulted == []
    assert not (target / "claude/.credentials.json").exists()


def test_claude_json_without_a_config_dir_is_read_from_the_home_dotfile(
    tmp_path: Path, target: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_agent_sdk._internal import session_resume

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(session_resume, "_read_keychain_credentials", lambda: None)
    (home / ".claude.json").write_bytes(b'{"primaryApiKey": "from-home-dotfile"}\n')
    (home / ".claude" / ".claude.json").write_bytes(b'{"primaryApiKey": "from-config-dir"}\n')
    _put(home / ".claude" / ".credentials.json", {"claudeAiOauth": {"accessToken": "home-at"}})

    _stage(target, {})

    assert (target / "claude/.claude.json").read_bytes() == (home / ".claude.json").read_bytes()


def test_an_unreadable_canonical_file_raises_and_leaves_the_staged_copy_untouched(
    canon: Canon, target: Path,
) -> None:
    _stage(target, canon.env)
    before = {rel: (target / rel).read_bytes() for rel in STAGED}

    (canon.claude / ".credentials.json").unlink()
    (canon.claude / ".credentials.json").mkdir()
    canon.write_codex(access=_jwt(FAR // 1000, "codex-changed"))

    with pytest.raises(OSError):
        stage_provider_auth(target, env=canon.env)

    assert {rel: (target / rel).read_bytes() for rel in STAGED} == before


def test_concurrent_stagers_never_expose_a_partial_file(canon: Canon, target: Path) -> None:
    _stage(target, canon.env)
    done = threading.Event()
    bad: list[str] = []

    def _guard(fn: Callable[[], None]) -> Callable[[], None]:
        def _run() -> None:
            try:
                fn()
            except Exception as exc:  # surfaced through `bad`, not a thread warning
                bad.append(repr(exc))
        return _run

    def _stage_many() -> None:
        for _ in range(40):
            stage_provider_auth(target, env=canon.env)

    def _churn() -> None:
        for i in range(80):
            canon.write_claude(access=f"claude-at-{i}")
            canon.write_codex(access=_jwt(FAR // 1000, f"codex-{i}"))

    def _read() -> None:
        while not done.is_set():
            for rel in ("claude/.credentials.json", "codex/auth.json"):
                try:
                    json.loads((target / rel).read_bytes())
                except ValueError as exc:
                    bad.append(f"{rel}: {exc}")

    reader = threading.Thread(target=_guard(_read))
    workers = [threading.Thread(target=_guard(f)) for f in (_stage_many, _stage_many, _churn)]
    reader.start()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    done.set()
    reader.join()
    assert bad == []


def test_a_waiting_reconcile_reads_canonical_only_once_it_holds_the_lock(
    canon: Canon, target: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_provider_auth_dir(target)
    holder = filelock.FileLock(str(target.with_name("provider-auth.lock")))
    holder.acquire()
    waiting = threading.Event()
    real_lock = pa._stage_lock

    def _spy(t: Path) -> filelock.BaseFileLock:
        waiting.set()
        return real_lock(t)

    monkeypatch.setattr(pa, "_stage_lock", _spy)
    worker = threading.Thread(
        target=stage_provider_auth, args=(target,), kwargs={"env": canon.env})
    try:
        worker.start()
        assert waiting.wait(10)
        canon.write_claude(access="claude-at-replaced")
    finally:
        holder.release()
    worker.join(10)
    assert not worker.is_alive()
    assert _staged(target, "claude/.credentials.json")["claudeAiOauth"][
        "accessToken"] == "claude-at-replaced"
