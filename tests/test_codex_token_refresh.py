from __future__ import annotations

import base64
import json
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
