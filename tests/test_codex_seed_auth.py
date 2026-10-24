from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers import codex_token_refresh as ctr
from imp.providers.codex_config import redacted_auth_json, seed_auth
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, mcp_call, thread_started, turn_completed
from tests.test_codex_runner_happy_path import _build_invocation


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _jwt(exp_s: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_s}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _canonical_payload(access_token: str = "at-1") -> dict[str, Any]:
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"refresh_token": "rt-1", "access_token": access_token,
                   "id_token": "id-1", "account_id": "acct-1"},
        "last_refresh": "2026-06-01T00:00:00Z",
    }


def test_seed_chatgpt_blanks_refresh_token_and_leaves_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical-auth.json"
    canonical.write_text(json.dumps(_canonical_payload()))
    before = canonical.read_bytes()
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)

    out = seed_auth(mode="chatgpt", canonical_path=canonical, codex_home=home, env={})

    assert out == home / "auth.json"
    expected = _canonical_payload()
    expected["tokens"]["refresh_token"] = ""
    assert json.loads(out.read_text()) == expected
    assert (out.stat().st_mode & 0o777) == 0o600
    assert canonical.read_bytes() == before


def test_seed_chatgpt_nulls_a_stored_api_key_and_leaves_canonical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical-auth.json"
    payload = _canonical_payload()
    payload["OPENAI_API_KEY"] = "sk-stored"
    canonical.write_text(json.dumps(payload))
    before = canonical.read_bytes()
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)

    out = seed_auth(mode="chatgpt", canonical_path=canonical, codex_home=home, env={})

    seeded = json.loads(out.read_text())
    assert "OPENAI_API_KEY" in seeded
    assert seeded["OPENAI_API_KEY"] is None
    assert canonical.read_bytes() == before


def test_redacted_auth_json_is_deterministic_and_sorted() -> None:
    forward = _canonical_payload()
    backward = dict(reversed(list(forward.items())))
    backward["tokens"] = dict(reversed(list(forward["tokens"].items())))

    expected = _canonical_payload()
    expected["tokens"]["refresh_token"] = ""
    assert redacted_auth_json(forward) == redacted_auth_json(backward)
    assert redacted_auth_json(forward) == json.dumps(
        expected, indent=2, sort_keys=True).encode("utf-8")


def test_redacted_auth_json_does_not_mutate_its_input() -> None:
    payload = _canonical_payload()
    redacted_auth_json(payload)
    assert payload["tokens"]["refresh_token"] == "rt-1"


def test_redacted_auth_json_without_tokens_object_passes_through() -> None:
    assert json.loads(redacted_auth_json({"auth_mode": "chatgpt"})) == {
        "auth_mode": "chatgpt"}


def test_redacted_auth_json_with_non_dict_tokens_passes_through() -> None:
    assert json.loads(redacted_auth_json({"auth_mode": "chatgpt", "tokens": None})) == {
        "auth_mode": "chatgpt", "tokens": None}


def test_seed_apikey_writes_fresh_auth(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    out = seed_auth(
        mode="apikey", canonical_path=tmp_path / "missing",
        codex_home=home, env={"OPENAI_API_KEY": "sk-real-key"},
    )
    body = json.loads(out.read_text())
    assert body["auth_mode"] == "apikey"
    assert body["OPENAI_API_KEY"] == "sk-real-key"
    assert (out.stat().st_mode & 0o777) == 0o600


@pytest.mark.anyio
async def test_codex_run_seeds_a_blank_refresh_token_and_leaves_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_token = _jwt(int(time.time()) + 24 * 3600)
    canonical = tmp_path / "auth.json"
    canonical.write_text(json.dumps(_canonical_payload(access_token)))
    before = canonical.read_bytes()
    (tmp_path / "tx").mkdir()
    # The pre-spawn refresh reads $CODEX_HOME; point it at the same canonical.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    http_calls: list[str] = []

    def _no_http(refresh_token: str) -> dict[str, Any]:
        http_calls.append(refresh_token)
        raise ctr.RefreshTransientError("stub")

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    seen: dict[str, Any] = {}
    scripted = [[
        thread_started("thr-1"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="step complete", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]

    def _factory(options: Any = None) -> FakeCodex:
        # Built after seed_auth and before teardown shreds CODEX_HOME.
        assert runner._codex_home is not None
        seen["auth"] = json.loads((runner._codex_home / "auth.json").read_text())
        return FakeCodex(scripted_turns=scripted)

    runner = CodexAgentRunner(model_name="gpt-5.5", client_factory=_factory)
    runner._canonical_auth_override = canonical  # test seam

    events = [ev async for ev in runner.run(
        _build_invocation(tmp_path, event_log=InMemoryEventLog()))]

    assert any(e.get("type") == "agent_result" for e in events), events
    assert seen["auth"]["tokens"]["refresh_token"] == ""
    assert seen["auth"]["tokens"]["access_token"] == access_token
    assert canonical.read_bytes() == before
    assert http_calls == []
