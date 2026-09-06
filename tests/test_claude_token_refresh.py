from __future__ import annotations

import contextlib
import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import filelock
import httpx
import pytest

from imp.providers import claude_token_refresh as ctr

NOW = 1_000_000_000_000


def _write_creds(
    base: Path,
    *,
    expires_at: int | None,
    refresh_token: str = "old-refresh",
    access_token: str = "old-access",
) -> Path:
    oauth: dict[str, Any] = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }
    if expires_at is not None:
        oauth["expiresAt"] = expires_at
    path = base / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


def _env(base: Path, **extra: str) -> dict[str, str]:
    env = {"CLAUDE_CONFIG_DIR": str(base)}
    env.update(extra)
    return env


def test_fresh_token_skips(tmp_path: Path) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 1000)
    before = path.read_bytes()
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH
    assert out.expires_at is None
    assert path.read_bytes() == before


def test_env_api_key_is_not_applicable(tmp_path: Path) -> None:
    _write_creds(tmp_path, expires_at=NOW - 1)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path, ANTHROPIC_API_KEY="sk-x"))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_env_oauth_token_is_not_applicable(tmp_path: Path) -> None:
    _write_creds(tmp_path, expires_at=NOW - 1)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="tok"))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_missing_file_is_not_applicable(tmp_path: Path) -> None:
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_torn_file_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text("{ not json", encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_no_refresh_token_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": NOW - 1}}),
        encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_non_dict_oauth_is_not_applicable(tmp_path: Path) -> None:
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": None}), encoding="utf-8")
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.NOT_APPLICABLE


def test_near_expiry_refreshes_and_writes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 60 * 1000)  # 1 min < buffer

    def _fake(refresh_token: str) -> dict[str, Any]:
        assert refresh_token == "old-refresh"
        return {"access_token": "new-access",
                "refresh_token": "new-refresh", "expires_in": 3600}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))

    assert out.outcome == ctr.REFRESHED
    assert out.expires_at == NOW + 3600 * 1000
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "new-access"
    assert oauth["refreshToken"] == "new-refresh"
    assert oauth["expiresAt"] == NOW + 3600 * 1000
    assert oauth["scopes"] == ["user:inference", "user:profile"]
    assert oauth["subscriptionType"] == "max"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_second_call_reuses_rotated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 60 * 1000)
    calls = {"n": 0}

    def _fake(refresh_token: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"access_token": "acc", "refresh_token": "rot",
                "expires_in": 3600}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    first = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    second = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))

    assert first.outcome == ctr.REFRESHED
    assert second.outcome == ctr.SKIPPED_FRESH
    assert calls["n"] == 1
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "acc"
    assert oauth["refreshToken"] == "rot"


def test_request_refresh_posts_form_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, timeout: Any = None) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def post(self, url: str, data: Any = None) -> httpx.Response:
            captured["url"] = url
            captured["data"] = data
            return httpx.Response(
                200,
                json={"access_token": "a", "refresh_token": "b",
                      "expires_in": 10},
                request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    result = ctr._request_refresh("the-refresh-token")

    assert result == {"access_token": "a", "refresh_token": "b",
                      "expires_in": 10}
    assert captured["url"] == ctr._TOKEN_URL
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "the-refresh-token",
        "client_id": ctr._CLIENT_ID,
    }


def test_request_refresh_malformed_200_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(200, json={"access_token": "a"},  # missing fields
                          request=httpx.Request("POST", ctr._TOKEN_URL))

    class _FakeClient:
        def __init__(self, timeout: Any = None) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def post(self, url: str, data: Any = None) -> httpx.Response:
            return resp

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def _raise_transient(refresh_token: str) -> dict[str, Any]:
    raise ctr.RefreshTransientError("network down")


def _raise_invalid_grant(refresh_token: str) -> dict[str, Any]:
    raise ctr.RefreshInvalidGrant()


def _client(*, response: Any = None, error: BaseException | None = None) -> Any:
    class _FakeClient:
        def __init__(self, timeout: Any = None) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def post(self, url: str, data: Any = None) -> Any:
            if error is not None:
                raise error
            return response

    return _FakeClient


def test_transient_error_proceeds_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW - 1)
    before = path.read_bytes()
    monkeypatch.setattr(ctr, "_request_refresh", _raise_transient)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_lock_timeout_proceeds_without_http_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW - 1)
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("no HTTP when the lock is unavailable")

    def _timeout(*a: Any, **k: Any) -> Any:
        raise ctr.LockTimeout()

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    monkeypatch.setattr(ctr, "_token_lock", _timeout)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_invalid_grant_proceeds_without_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW - 1, refresh_token="dead")
    before = path.read_bytes()
    monkeypatch.setattr(ctr, "_request_refresh", _raise_invalid_grant)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_INVALID_GRANT
    assert path.read_bytes() == before


def test_failure_never_mutates_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW - 1, refresh_token="keep-me")
    before = path.read_bytes()
    monkeypatch.setattr(ctr, "_request_refresh", _raise_transient)
    ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert path.read_bytes() == before
    monkeypatch.setattr(ctr, "_request_refresh", _raise_invalid_grant)
    ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert path.read_bytes() == before


def test_request_refresh_invalid_grant_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = httpx.Response(400, json={"error": "invalid_grant"},
                          request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(response=resp))
    with pytest.raises(ctr.RefreshInvalidGrant):
        ctr._request_refresh("dead")


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
    err = httpx.ConnectError(
        "boom", request=httpx.Request("POST", ctr._TOKEN_URL))
    monkeypatch.setattr(httpx, "Client", _client(error=err))
    with pytest.raises(ctr.RefreshTransientError):
        ctr._request_refresh("tok")


def test_real_lock_timeout_maps_to_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW - 1)
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, Any]:
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
    path = _write_creds(tmp_path, expires_at=NOW - 1)
    before = path.read_bytes()

    def _fake_refresh(refresh_token: str) -> dict[str, Any]:
        return {"access_token": "new-access", "refresh_token": "new-refresh",
                "expires_in": 3600}

    def _raise_oserror(creds_path: Path, data: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ctr, "_request_refresh", _fake_refresh)
    monkeypatch.setattr(ctr, "_atomic_write_creds", _raise_oserror)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_under_lock_recheck_skips_when_peer_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = {"claudeAiOauth": {
        "accessToken": "old", "refreshToken": "old-r",
        "expiresAt": NOW - 1, "scopes": [], "subscriptionType": "max"}}
    fresh = {"claudeAiOauth": {
        "accessToken": "peer", "refreshToken": "peer-r",
        "expiresAt": NOW + 10 * 60 * 1000, "scopes": [],
        "subscriptionType": "max"}}
    # First read (pre-lock) sees the stale token; the read under the lock sees
    # the token a peer refreshed while we waited for the lock.
    seq = [stale, fresh]

    def _load(creds_path: Path) -> dict[str, Any]:
        return seq.pop(0) if seq else fresh

    def _no_http(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("peer already refreshed; no HTTP expected")

    monkeypatch.setattr(ctr, "_load_credentials", _load)
    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH


def test_unresolvable_home_is_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(ctr.Path, "home", staticmethod(_boom))
    out = ctr.refresh_canonical_token(NOW, env={})
    assert out.outcome == ctr.NOT_APPLICABLE


def test_force_refreshes_a_far_from_expiry_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 10h out: reaching the endpoint proves both _needs_refresh checks were skipped.
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)

    def _fake(refresh_token: str) -> dict[str, Any]:
        assert refresh_token == "old-refresh"
        return {"access_token": "forced-access",
                "refresh_token": "forced-refresh", "expires_in": 3600}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path), force=True)

    assert out.outcome == ctr.REFRESHED
    assert out.expires_at == NOW + 3600 * 1000
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "forced-access"
    assert oauth["refreshToken"] == "forced-refresh"
    assert oauth["expiresAt"] == NOW + 3600 * 1000
    assert oauth["subscriptionType"] == "max"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unforced_default_still_skips_a_fresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("an unforced fresh token must not hit the endpoint")

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path))
    assert out.outcome == ctr.SKIPPED_FRESH
    assert path.read_bytes() == before


def test_force_still_reports_invalid_grant_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000,
                        refresh_token="dead")
    before = path.read_bytes()
    monkeypatch.setattr(ctr, "_request_refresh", _raise_invalid_grant)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path), force=True)
    assert out.outcome == ctr.FAILED_INVALID_GRANT
    assert path.read_bytes() == before


def test_force_still_reports_a_lock_timeout_as_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("no HTTP when the lock is unavailable")

    def _timeout(*a: Any, **k: Any) -> Any:
        raise ctr.LockTimeout()

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    monkeypatch.setattr(ctr, "_token_lock", _timeout)
    out = ctr.refresh_canonical_token(NOW, env=_env(tmp_path), force=True)
    assert out.outcome == ctr.FAILED_TRANSIENT
    assert path.read_bytes() == before


def test_force_with_an_api_key_env_is_not_applicable(tmp_path: Path) -> None:
    _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path, ANTHROPIC_API_KEY="sk-x"), force=True)
    assert out.outcome == ctr.NOT_APPLICABLE


_UNREADABLE_EXPIRIES = [
    pytest.param({}, id="missing"),
    pytest.param({"expiresAt": True}, id="boolean"),
    pytest.param({"expiresAt": "soon"}, id="non-numeric"),
    pytest.param({"expiresAt": float("nan")}, id="nan"),
    pytest.param({"expiresAt": float("inf")}, id="inf"),
]


def test_expires_at_of_reads_a_numeric_expiry() -> None:
    assert ctr._expires_at_of({"expiresAt": NOW + 1000}) == NOW + 1000
    assert ctr._expires_at_of({"expiresAt": float(NOW + 1000)}) == NOW + 1000


@pytest.mark.parametrize("oauth", _UNREADABLE_EXPIRIES)
def test_expires_at_of_is_none_for_an_unreadable_expiry(oauth: dict[str, Any]) -> None:
    assert ctr._expires_at_of(oauth) is None


@pytest.mark.parametrize("oauth", _UNREADABLE_EXPIRIES)
def test_an_unreadable_expiry_needs_refresh_and_never_raises(
    oauth: dict[str, Any],
) -> None:
    assert ctr._needs_refresh(oauth, NOW) is True


def test_needs_refresh_keeps_the_five_minute_buffer() -> None:
    assert ctr._needs_refresh({"expiresAt": NOW + 10 * 60 * 1000}, NOW) is False
    assert ctr._needs_refresh({"expiresAt": NOW + 5 * 60 * 1000}, NOW) is True
    assert ctr._needs_refresh({"expiresAt": NOW - 1}, NOW) is True


_ABSENT = object()


def _write_creds_expiry(base: Path, value: Any) -> Path:
    """`json.dumps` emits a bare NaN literal, which `json.loads` accepts on the way back."""
    oauth: dict[str, Any] = {
        "accessToken": "old-access",
        "refreshToken": "old-refresh",
        "subscriptionType": "max",
    }
    if value is not _ABSENT:
        oauth["expiresAt"] = value
    path = base / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


def test_a_moved_expiry_skips_the_forced_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)
    before = path.read_bytes()

    def _no_http(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("a peer already rotated; no HTTP expected")

    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True,
        spawn_expires_at=NOW + 9 * 60 * 60 * 1000)

    assert out.outcome == ctr.SKIPPED_ROTATED
    assert out.expires_at is None
    assert path.read_bytes() == before


def test_a_peer_rotating_while_we_wait_for_the_lock_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison must sit inside the lock: a rotation that lands while this call
    waits for the lock is invisible to any pre-lock read."""
    spawn_expiry = NOW + 10 * 60 * 60 * 1000
    path = _write_creds(tmp_path, expires_at=spawn_expiry)

    @contextlib.contextmanager
    def _lock_that_rotates(lock_path: Path, *, timeout: float) -> Iterator[None]:
        _write_creds(tmp_path, expires_at=spawn_expiry + 1,
                     refresh_token="peer-refresh", access_token="peer-access")
        yield

    def _no_http(refresh_token: str) -> dict[str, Any]:
        raise AssertionError("the peer's rotation must be seen under the lock")

    monkeypatch.setattr(ctr, "_token_lock", _lock_that_rotates)
    monkeypatch.setattr(ctr, "_request_refresh", _no_http)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True, spawn_expires_at=spawn_expiry)

    assert out.outcome == ctr.SKIPPED_ROTATED
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "peer-access"
    assert oauth["expiresAt"] == spawn_expiry + 1


def test_no_spawn_expiry_still_rotates_a_far_from_expiry_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)

    def _fake(refresh_token: str) -> dict[str, Any]:
        return {"access_token": "forced-access",
                "refresh_token": "forced-refresh", "expires_in": 3600}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True, spawn_expires_at=None)

    assert out.outcome == ctr.REFRESHED
    assert out.expires_at == NOW + 3600 * 1000
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "forced-access"
    assert oauth["expiresAt"] == NOW + 3600 * 1000


@pytest.mark.parametrize("value", [
    pytest.param(_ABSENT, id="missing"),
    pytest.param(True, id="boolean"),
    pytest.param("soon", id="non-numeric"),
    pytest.param(float("nan"), id="nan"),
])
def test_an_unreadable_under_lock_expiry_probes_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any,
) -> None:
    path = _write_creds_expiry(tmp_path, value)
    calls: list[str] = []

    def _fake(refresh_token: str) -> dict[str, Any]:
        calls.append(refresh_token)
        return {"access_token": "probe-access",
                "refresh_token": "probe-refresh", "expires_in": 3600}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True,
        spawn_expires_at=NOW + 10 * 60 * 60 * 1000)

    assert out.outcome == ctr.REFRESHED
    assert calls == ["old-refresh"]
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "probe-access"


def test_a_matching_expiry_still_rotates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_expiry = NOW + 10 * 60 * 60 * 1000
    path = _write_creds(tmp_path, expires_at=spawn_expiry)

    def _fake(refresh_token: str) -> dict[str, Any]:
        return {"access_token": "probe-access",
                "refresh_token": "probe-refresh", "expires_in": 3600}

    monkeypatch.setattr(ctr, "_request_refresh", _fake)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True, spawn_expires_at=spawn_expiry)

    assert out.outcome == ctr.REFRESHED
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    assert oauth["accessToken"] == "probe-access"


def test_a_matching_expiry_still_reports_a_dead_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_expiry = NOW + 10 * 60 * 60 * 1000
    path = _write_creds(tmp_path, expires_at=spawn_expiry, refresh_token="dead")
    before = path.read_bytes()

    monkeypatch.setattr(ctr, "_request_refresh", _raise_invalid_grant)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path), force=True, spawn_expires_at=spawn_expiry)

    assert out.outcome == ctr.FAILED_INVALID_GRANT
    assert path.read_bytes() == before


def test_the_env_guard_precedes_the_rotation_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_creds(tmp_path, expires_at=NOW + 10 * 60 * 60 * 1000)

    def _no_read(creds_path: Path) -> dict[str, Any]:
        raise AssertionError("the env guard must return before any credential read")

    monkeypatch.setattr(ctr, "_load_credentials", _no_read)
    out = ctr.refresh_canonical_token(
        NOW, env=_env(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="tok"), force=True,
        spawn_expires_at=NOW + 9 * 60 * 60 * 1000)

    assert out.outcome == ctr.NOT_APPLICABLE
