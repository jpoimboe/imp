from __future__ import annotations

import base64
import json
import httpx
import pytest
from datetime import datetime, timezone

from imp.providers import codex_token_refresh as ctr

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
        datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    just_inside = (
        datetime.fromtimestamp((cutoff_ms + 5_000) / 1000, tz=timezone.utc)
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

        def __enter__(self) -> "_FakeClient":
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

        def __enter__(self) -> "_FakeClient":
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
