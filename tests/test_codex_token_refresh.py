from __future__ import annotations

import base64
import contextlib
import json
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import filelock
import httpx
import pytest

from imp.providers import codex_token_refresh as ctr
from imp.providers.codex_config import detect_auth_mode

NOW = 1_000_000_000_000  # ms


def _jwt(exp: object) -> str:
    """A dot-delimited JWT whose payload segment carries `exp` (no real signature)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_obj = {} if exp is None else {"exp": exp}
    payload = base64.urlsafe_b64encode(
        json.dumps(payload_obj).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def test_jwt_exp_ms_reads_exp_as_unix_seconds() -> None:
    assert ctr._jwt_exp_ms(_jwt(1_700_000_000)) == 1_700_000_000 * 1000


def test_jwt_exp_ms_none_for_non_jwt() -> None:
    assert ctr._jwt_exp_ms("opaque-token") is None
    assert ctr._jwt_exp_ms("") is None
    assert ctr._jwt_exp_ms(None) is None
    assert ctr._jwt_exp_ms(12345) is None


def test_jwt_exp_ms_none_when_exp_absent_or_malformed() -> None:
    assert ctr._jwt_exp_ms(_jwt(None)) is None          # payload has no exp
    assert ctr._jwt_exp_ms(_jwt("soon")) is None         # exp not numeric
    assert ctr._jwt_exp_ms("a.!!!notbase64!!!.c") is None


def test_jwt_exp_ms_none_for_boolean_exp() -> None:
    assert ctr._jwt_exp_ms(_jwt(True)) is None
    assert ctr._jwt_exp_ms(_jwt(False)) is None


def test_needs_refresh_uses_jwt_exp_with_5min_buffer() -> None:
    # exp far in the future → fresh.
    far = {"access_token": _jwt(NOW // 1000 + 3600)}
    assert ctr._needs_refresh(far, None, NOW) is False
    # exp inside the 5-min buffer → refresh.
    near = {"access_token": _jwt(NOW // 1000 + 60)}
    assert ctr._needs_refresh(near, None, NOW) is True
    # exp exactly at now + buffer → refresh (<=).
    edge = {"access_token": _jwt((NOW + ctr._BUFFER_MS) // 1000)}
    assert ctr._needs_refresh(edge, None, NOW) is True


def test_needs_refresh_falls_back_to_last_refresh_when_no_exp() -> None:
    tokens = {"access_token": "opaque"}  # no parseable exp
    recent = "2001-09-09T01:30:00Z"      # ~within 8 days of NOW
    old = "2001-08-01T00:00:00Z"         # > 8 days before NOW
    # NOW = 1_000_000_000_000 ms == 2001-09-09T01:46:40Z.
    assert ctr._needs_refresh(tokens, recent, NOW) is False
    assert ctr._needs_refresh(tokens, old, NOW) is True


def test_needs_refresh_true_when_neither_exp_nor_last_refresh() -> None:
    assert ctr._needs_refresh({"access_token": "opaque"}, None, NOW) is True
    assert ctr._needs_refresh({}, "not-a-date", NOW) is True


def test_needs_refresh_last_refresh_8day_boundary() -> None:
    tokens = {"access_token": "opaque"}
    cutoff_ms = NOW - ctr._REFRESH_INTERVAL_DAYS * ctr._DAY_MS
    exact_cutoff = (
        datetime.fromtimestamp(cutoff_ms / 1000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    just_inside = (
        datetime.fromtimestamp((cutoff_ms + 5_000) / 1000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    assert ctr._needs_refresh(tokens, exact_cutoff, NOW) is True
    assert ctr._needs_refresh(tokens, just_inside, NOW) is False


def test_parse_last_refresh_ms_naive_treated_as_utc() -> None:
    naive = ctr._parse_last_refresh_ms("2001-09-09T01:30:00")
    utc = ctr._parse_last_refresh_ms("2001-09-09T01:30:00Z")
    assert naive is not None
    assert naive == utc


def _client(*, response: object = None, error: BaseException | None = None) -> object:
    class _FakeClient:
        def __init__(self, timeout: object = None) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def post(self, url: str, json: object = None) -> object:
            if error is not None:
                raise error
            return response

    return _FakeClient


def test_request_refresh_posts_json_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, timeout: object = None) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def post(self, url: str, json: object = None) -> httpx.Response:
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(
                200,
                json={"id_token": "i", "access_token": "a", "refresh_token": "b"},
                request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    result = ctr._request_refresh("the-refresh-token")

    assert result == {"id_token": "i", "access_token": "a", "refresh_token": "b"}
    assert captured["url"] == ctr._TOKEN_URL
    assert captured["json"] == {
        "client_id": ctr._CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "the-refresh-token",
    }


def test_request_refresh_optional_fields_default_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(200, json={"access_token": "a"},
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    assert ctr._request_refresh("tok") == {
        "id_token": None, "access_token": "a", "refresh_token": None}


def test_request_refresh_401_is_invalid_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(401, text="unauthorized",
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshInvalidGrant):
        ctr._request_refresh("dead")


@pytest.mark.parametrize(
    "code", ["refresh_token_expired", "refresh_token_reused",
             "refresh_token_invalidated"])
def test_request_refresh_dead_codes_nested(
    monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    resp = httpx.Response(400, json={"error": {"code": code}},
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshInvalidGrant):
        ctr._request_refresh("dead")


@pytest.mark.parametrize(
    "code", ["refresh_token_expired", "refresh_token_reused",
             "refresh_token_invalidated"])
def test_request_refresh_dead_codes_top_level(
    monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    # top-level `error` string shape
    resp = httpx.Response(400, json={"error": code},
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshInvalidGrant):
        ctr._request_refresh("dead")
    # top-level `code` string shape
    resp2 = httpx.Response(400, json={"code": code},
                           request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp2))
    with pytest.raises(ctr.RefreshInvalidGrant):
        ctr._request_refresh("dead")


def test_request_refresh_unrecognized_400_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(400, json={"error": "invalid_request"},
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def test_request_refresh_server_error_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(503, text="busy",
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def test_request_refresh_network_error_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = httpx.ConnectError("boom", request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(error=err))
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def test_request_refresh_malformed_200_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # valid JSON but not an object → malformed.
    resp = httpx.Response(200, json=["not", "an", "object"],
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def test_request_refresh_non_json_200_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(200, content=b"<html>not json</html>",
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def test_endpoint_constants_are_pinned() -> None:
    assert ctr._TOKEN_URL == "https://auth.openai.com/oauth/token"
    assert ctr._CLIENT_ID == "app_EMoamEEZ73f0CkXaXp7hrann"


def _write_auth(
    base: Path,
    *,
    access_token: str = "opaque-access",
    refresh_token: str = "old-refresh",
    last_refresh: str | None = None,
    extra_top: dict[str, object] | None = None,
    extra_tokens: dict[str, object] | None = None,
) -> Path:
    tokens: dict[str, object] = {
        "id_token": "old-id",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": "acct-123",
    }
    if extra_tokens:
        tokens.update(extra_tokens)
    payload: dict[str, object] = {"OPENAI_API_KEY": None, "tokens": tokens}
    if last_refresh is not None:
        payload["last_refresh"] = last_refresh
    if extra_top:
        payload.update(extra_top)
    path = base / "auth.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _env(base: Path, **extra: str) -> dict[str, str]:
    env = {"CODEX_HOME": str(base)}
    env.update(extra)
    return env


def test_canonical_dir_honors_codex_home_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ctr.canonical_dir({"CODEX_HOME": str(tmp_path)}) == tmp_path
    monkeypatch.setattr(ctr.Path, "home", staticmethod(lambda: tmp_path))
    assert ctr.canonical_dir({}) == tmp_path / ".codex"


def test_missing_file_is_not_applicable(tmp_path: Path) -> None:
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_torn_file_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text("{ not json", encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_apikey_body_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-x", "auth_mode": "apikey"}),
        encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_apikey_env_is_not_applicable(tmp_path: Path) -> None:
    _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1))
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path, OPENAI_API_KEY="sk-x"))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_auth_mode_apikey_field_is_not_applicable(tmp_path: Path) -> None:
    _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1),
                extra_top={"auth_mode": "apikey"})
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_no_tokens_object_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": None}), encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_no_refresh_token_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "a", "refresh_token": ""}}),
        encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_fresh_exp_skips(tmp_path: Path) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600))
    before = path.read_bytes()
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH
    assert out.expires_at is None
    assert path.read_bytes() == before


def test_recent_last_refresh_skips(tmp_path: Path) -> None:
    # NOW == 2001-09-09T01:46:40Z; last_refresh ~15 min earlier, no parseable exp.
    path = _write_auth(tmp_path, access_token="opaque",
                       last_refresh="2001-09-09T01:30:00Z")
    before = path.read_bytes()
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH
    assert path.read_bytes() == before


def test_near_expiry_refreshes_and_writes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(
        tmp_path, access_token=_jwt(NOW // 1000 + 60),  # inside 5-min buffer
        refresh_token="old-refresh",
        last_refresh="2001-01-01T00:00:00Z",
        extra_top={"account_id": "top-acct"})
    new_access = _jwt(NOW // 1000 + 7200)

    def _fake(refresh_token: str) -> dict[str, object]:
        assert refresh_token == "old-refresh"
        return {"id_token": "new-id", "access_token": new_access,
                "refresh_token": "new-refresh"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))

    assert out.outcome == ctr.REFRESHED
    assert out.expires_at == (NOW // 1000 + 7200) * 1000
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tokens"]["id_token"] == "new-id"
    assert data["tokens"]["access_token"] == new_access
    assert data["tokens"]["refresh_token"] == "new-refresh"
    assert data["tokens"]["account_id"] == "acct-123"      # preserved
    assert data["account_id"] == "top-acct"                # top-level preserved
    assert data["OPENAI_API_KEY"] is None                  # preserved
    assert data["last_refresh"] != "2001-01-01T00:00:00Z"  # advanced
    assert ctr._parse_last_refresh_ms(data["last_refresh"]) is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_refresh_updates_only_returned_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 60),
                       refresh_token="old-refresh")

    def _fake(refresh_token: str) -> dict[str, object]:
        # only refresh_token returned; id_token/access_token absent (None).
        return {"id_token": None, "access_token": None,
                "refresh_token": "rotated"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.REFRESHED
    tokens = json.loads(path.read_text(encoding="utf-8"))["tokens"]
    assert tokens["refresh_token"] == "rotated"
    assert tokens["id_token"] == "old-id"                       # untouched
    assert tokens["access_token"] == _jwt(NOW // 1000 + 60)     # untouched


def test_invalid_grant_proceeds_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1),
                       refresh_token="dead")
    before = path.read_bytes()

    def _dead(refresh_token: str) -> dict[str, object]:
        raise ctr.RefreshInvalidGrant()

    monkeypatch.setattr(ctr, "_request_refresh", _dead)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_INVALID_GRANT
    assert path.read_bytes() == before


def test_transient_error_proceeds_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1))
    before = path.read_bytes()

    def _boom(refresh_token: str) -> dict[str, object]:
        raise ctr.RefreshTransientError("network down")

    monkeypatch.setattr(ctr, "_request_refresh", _boom)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_real_lock_timeout_maps_to_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1))
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, object]:
        raise AssertionError("no HTTP while a peer holds the lock")

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    monkeypatch.setattr(ctr, "_LOCK_TIMEOUT", 0.1)
    held = filelock.FileLock(str(tmp_path / ctr._LOCK_FILE))
    held.acquire()
    try:
        out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    finally:
        held.release()
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_write_oserror_maps_to_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1))
    before = path.read_bytes()

    def _fake(refresh_token: str) -> dict[str, object]:
        return {"id_token": "i", "access_token": "a", "refresh_token": "b"}

    def _raise_oserror(auth_path: Path, data: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    monkeypatch.setattr(ctr, "_atomic_write_auth", _raise_oserror)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_under_lock_recheck_skips_when_peer_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = {"OPENAI_API_KEY": None, "tokens": {
        "access_token": _jwt(NOW // 1000 - 1), "refresh_token": "old-r"}}
    fresh = {"OPENAI_API_KEY": None, "tokens": {
        "access_token": _jwt(NOW // 1000 + 3600), "refresh_token": "peer-r"}}
    # First (pre-lock) read sees the stale token; the read under the lock sees
    # the token a peer refreshed while we waited for the lock.
    seq = [stale, fresh]

    def _load(auth_path: Path) -> dict[str, object]:
        return seq.pop(0) if seq else fresh

    def _no_http(refresh_token: str) -> dict[str, object]:
        raise AssertionError("peer already refreshed; no HTTP expected")

    monkeypatch.setattr(ctr, "_load_auth", _load)
    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH


def test_second_call_reuses_rotated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 60))
    calls = {"n": 0}

    def _fake(refresh_token: str) -> dict[str, object]:
        calls["n"] += 1
        return {"id_token": "i", "access_token": _jwt(NOW // 1000 + 7200),
                "refresh_token": "rot"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    first = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    second = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))

    assert first.outcome == ctr.REFRESHED
    assert second.outcome == ctr.SKIPPED_FRESH
    assert calls["n"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == "rot"


def test_unresolvable_home_is_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(ctr.Path, "home", staticmethod(_boom))
    out = ctr.refresh_canonical_token(NOW, env={})
    assert out.outcome == ctr.NOT_APPLICABLE


# Every row of the refresh/spawn agreement table: the file's explicit auth_mode
# is authoritative in both directions, key-presence only fills a missing field.
_AGREEMENT_ROWS = [
    pytest.param({"auth_mode": "chatgpt"}, {}, "chatgpt", ctr.REFRESHED, 1,
                 id="chatgpt-file-no-env-key"),
    pytest.param({"auth_mode": "chatgpt"}, {"OPENAI_API_KEY": "sk-x"}, "chatgpt",
                 ctr.REFRESHED, 1, id="chatgpt-file-env-key-set"),
    pytest.param({}, {"OPENAI_API_KEY": "sk-x"}, "apikey", ctr.NOT_APPLICABLE, 0,
                 id="no-auth-mode-field-env-key-set"),
    pytest.param({"auth_mode": "apikey"}, {}, "apikey", ctr.NOT_APPLICABLE, 0,
                 id="apikey-file-no-env-key"),
]


@pytest.mark.parametrize(
    "extra_top,extra_env,spawn_mode,expected_outcome,expected_calls",
    _AGREEMENT_ROWS)
def test_refresh_agrees_with_detect_auth_mode_on_every_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_top: dict[str, object],
    extra_env: dict[str, str],
    spawn_mode: str,
    expected_outcome: str,
    expected_calls: int,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 - 1),
                       extra_top=extra_top)
    seen: list[str] = []

    def _fake(refresh_token: str) -> dict[str, object]:
        seen.append(refresh_token)
        return {"id_token": None, "access_token": _jwt(NOW // 1000 + 7200),
                "refresh_token": "rot"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    env = _env(tmp_path, **extra_env)

    assert detect_auth_mode(canonical_path=path, env=env) == spawn_mode
    out = ctr.refresh_canonical_token(NOW, env=env)
    assert out.outcome == expected_outcome
    assert len(seen) == expected_calls


def test_force_refreshes_a_fresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An hour out with a minutes-old last_refresh: reaching the endpoint proves
    # both _needs_refresh short-circuits (pre-lock and under-lock) were skipped.
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600),
                       refresh_token="old-refresh",
                       last_refresh="2001-09-09T01:30:00Z")
    new_access = _jwt(NOW // 1000 + 7200)
    seen: list[str] = []

    def _fake(refresh_token: str) -> dict[str, object]:
        seen.append(refresh_token)
        return {"id_token": "new-id", "access_token": new_access,
                "refresh_token": "new-refresh"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path), force=True)

    assert out.outcome == ctr.REFRESHED
    assert out.expires_at == (NOW // 1000 + 7200) * 1000
    assert seen == ["old-refresh"]
    tokens = json.loads(path.read_text(encoding="utf-8"))["tokens"]
    assert tokens["access_token"] == new_access
    assert tokens["refresh_token"] == "new-refresh"


def test_force_still_reports_invalid_grant_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600),
                       refresh_token="dead")
    before = path.read_bytes()

    def _dead(refresh_token: str) -> dict[str, object]:
        raise ctr.RefreshInvalidGrant()

    monkeypatch.setattr(ctr, "_request_refresh", _dead)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path), force=True)

    assert out.outcome == ctr.FAILED_INVALID_GRANT
    assert path.read_bytes() == before


def test_a_moved_expiry_skips_the_forced_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600))
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, object]:
        raise AssertionError("a peer already rotated; no HTTP expected")

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True,
        spawn_expires_at=(NOW // 1000 + 1800) * 1000)

    assert out.outcome == ctr.SKIPPED_ROTATED
    assert out.expires_at is None
    assert path.read_bytes() == before


def test_a_peer_rotating_while_we_wait_for_the_lock_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotation landing while we wait for the lock is invisible pre-lock."""
    spawn_expiry = (NOW // 1000 + 3600) * 1000
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600))

    @contextlib.contextmanager
    def _lock_that_rotates(lock_path: Path, *, timeout: float) -> Iterator[None]:
        _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 7200),
                    refresh_token="peer-refresh")
        yield

    def _no_http(refresh_token: str) -> dict[str, object]:
        raise AssertionError("the peer's rotation must be seen under the lock")

    monkeypatch.setattr(ctr, "_token_lock", _lock_that_rotates)
    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True, spawn_expires_at=spawn_expiry)

    assert out.outcome == ctr.SKIPPED_ROTATED
    tokens = json.loads(path.read_text(encoding="utf-8"))["tokens"]
    assert tokens["refresh_token"] == "peer-refresh"   # the peer's, unspent


def test_a_matching_expiry_still_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_expiry = (NOW // 1000 + 3600) * 1000
    path = _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600))
    seen: list[str] = []

    def _fake(refresh_token: str) -> dict[str, object]:
        seen.append(refresh_token)
        return {"id_token": None, "access_token": _jwt(NOW // 1000 + 7200),
                "refresh_token": "probe-refresh"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True, spawn_expires_at=spawn_expiry)

    assert out.outcome == ctr.REFRESHED
    assert seen == ["old-refresh"]
    tokens = json.loads(path.read_text(encoding="utf-8"))["tokens"]
    assert tokens["refresh_token"] == "probe-refresh"


@pytest.mark.parametrize("access_token,spawn_expires_at", [
    pytest.param("opaque-access", (NOW // 1000 + 3600) * 1000,
                 id="on-disk-expiry-unreadable"),
    pytest.param(_jwt(NOW // 1000 + 3600), None,
                 id="spawn-expiry-unknown"),
])
def test_an_unreadable_expiry_refreshes_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    access_token: str, spawn_expires_at: int | None,
) -> None:
    path = _write_auth(tmp_path, access_token=access_token)
    seen: list[str] = []

    def _fake(refresh_token: str) -> dict[str, object]:
        seen.append(refresh_token)
        return {"id_token": None, "access_token": _jwt(NOW // 1000 + 7200),
                "refresh_token": "probe-refresh"}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True,
        spawn_expires_at=spawn_expires_at)

    assert out.outcome == ctr.REFRESHED
    assert seen == ["old-refresh"]
    tokens = json.loads(path.read_text(encoding="utf-8"))["tokens"]
    assert tokens["refresh_token"] == "probe-refresh"


def test_canonical_expires_at_reads_the_access_token_jwt(tmp_path: Path) -> None:
    _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600))
    assert ctr.canonical_expires_at(env=_env(tmp_path)) == (NOW // 1000 + 3600) * 1000


def test_canonical_expires_at_still_reads_with_an_api_key_exported(
    tmp_path: Path,
) -> None:
    # Rotation detection must not go blind for a chatgpt file with a key exported.
    _write_auth(tmp_path, access_token=_jwt(NOW // 1000 + 3600),
                extra_top={"auth_mode": "chatgpt"})
    env = _env(tmp_path, OPENAI_API_KEY="sk-x")
    assert ctr.canonical_expires_at(env=env) == (NOW // 1000 + 3600) * 1000


@pytest.mark.parametrize("payload", [
    pytest.param(None, id="no-file"),
    pytest.param({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}, id="apikey-body"),
    pytest.param({"tokens": {"access_token": "opaque", "refresh_token": "r"}},
                 id="opaque-access-token"),
    pytest.param({"tokens": "not-an-object"}, id="tokens-not-an-object"),
])
def test_canonical_expires_at_is_none_when_unreadable(
    tmp_path: Path, payload: dict[str, object] | None,
) -> None:
    if payload is not None:
        (tmp_path / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
    assert ctr.canonical_expires_at(env=_env(tmp_path)) is None


def test_canonical_expires_at_is_none_when_home_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(ctr.Path, "home", staticmethod(_boom))
    assert ctr.canonical_expires_at(env={}) is None
