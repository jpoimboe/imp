from __future__ import annotations

import base64
import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers import claude as claude_provider
from imp.providers import claude_token_refresh as claude_ctr
from imp.providers import codex_runner
from imp.providers import codex_token_refresh as codex_ctr

NOW = 1_000_000_000_000  # ms; far in the past relative to wall-clock time


def _jwt(exp_s: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_s}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _read_only_dir(path: Path, name: str, body: dict[str, Any]) -> Iterator[Path]:
    path.mkdir(parents=True)
    f = path / name
    f.write_text(json.dumps(body, indent=2, sort_keys=True))
    f.chmod(stat.S_IRUSR | stat.S_IWUSR)
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        yield path
    finally:
        path.chmod(stat.S_IRWXU)


@pytest.fixture
def staged_claude(tmp_path: Path) -> Iterator[Path]:
    yield from _read_only_dir(
        tmp_path / "provider-auth" / "claude",
        ".credentials.json",
        {
            "claudeAiOauth": {
                "accessToken": "staged-access",
                "expiresAt": NOW - 1,
                "scopes": ["user:inference"],
            }
        },
    )


@pytest.fixture
def staged_codex(tmp_path: Path) -> Iterator[Path]:
    yield from _read_only_dir(
        tmp_path / "provider-auth" / "codex",
        "auth.json",
        {
            "auth_mode": "chatgpt",
            "last_refresh": "2001-01-01T00:00:00Z",
            "tokens": {"access_token": _jwt(NOW // 1000 - 1), "refresh_token": ""},
        },
    )


@pytest.fixture
def http_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _claude(refresh_token: str) -> dict[str, Any]:
        calls.append("claude")
        raise claude_ctr.RefreshTransientError("stub")

    def _codex(refresh_token: str) -> dict[str, Any]:
        calls.append("codex")
        raise codex_ctr.RefreshTransientError("stub")

    monkeypatch.setattr(claude_ctr, "_request_refresh", _claude)
    monkeypatch.setattr(codex_ctr, "_request_refresh", _codex)
    return calls


def _listing(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


@pytest.mark.parametrize("force", [False, True])
def test_claude_refresh_is_inert_on_read_only_staged_copy(
    staged_claude: Path,
    http_calls: list[str],
    force: bool,
) -> None:
    before = (staged_claude / ".credentials.json").read_bytes()
    out = claude_ctr.refresh_canonical_token(
        NOW,
        env={"CLAUDE_CONFIG_DIR": str(staged_claude)},
        force=force,
        spawn_expires_at=NOW - 1 if force else None,
    )
    assert out.outcome == claude_ctr.NOT_APPLICABLE
    assert http_calls == []
    assert _listing(staged_claude) == [".credentials.json"]
    assert (staged_claude / ".credentials.json").read_bytes() == before


@pytest.mark.parametrize("force", [False, True])
def test_codex_refresh_is_inert_on_read_only_staged_copy(
    staged_codex: Path,
    http_calls: list[str],
    force: bool,
) -> None:
    before = (staged_codex / "auth.json").read_bytes()
    out = codex_ctr.refresh_canonical_token(
        NOW,
        env={"CODEX_HOME": str(staged_codex)},
        force=force,
        spawn_expires_at=(NOW // 1000 - 1) * 1000 if force else None,
    )
    assert out.outcome == codex_ctr.NOT_APPLICABLE
    assert http_calls == []
    assert _listing(staged_codex) == ["auth.json"]
    assert (staged_codex / "auth.json").read_bytes() == before


def test_claude_pre_spawn_refresh_is_inert_on_read_only_staged_copy(
    staged_claude: Path,
    http_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(staged_claude))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    log = InMemoryEventLog()
    before = (staged_claude / ".credentials.json").read_bytes()

    claude_provider._pre_spawn_token_refresh(log, "step:t")

    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in events] == [{"outcome": "not_applicable"}]
    assert http_calls == []
    assert _listing(staged_claude) == [".credentials.json"]
    assert (staged_claude / ".credentials.json").read_bytes() == before


def test_codex_pre_spawn_refresh_is_inert_on_read_only_staged_copy(
    staged_codex: Path,
    http_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(staged_codex))
    log = InMemoryEventLog()
    before = (staged_codex / "auth.json").read_bytes()

    out = codex_runner._pre_spawn_token_refresh(log, "step:t")

    assert out.outcome == codex_ctr.NOT_APPLICABLE
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in events] == [{"outcome": "not_applicable"}]
    assert http_calls == []
    assert _listing(staged_codex) == ["auth.json"]
    assert (staged_codex / "auth.json").read_bytes() == before
