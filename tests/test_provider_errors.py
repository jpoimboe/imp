from __future__ import annotations

import pytest

from imp.providers import errors as perr


def test_subclass_categories() -> None:
    assert perr.AuthError("x").category == "auth"
    assert perr.ConfigError("x").category == "config"
    assert perr.QuotaError("x").category == "quota"
    assert perr.ContentFilterError("x").category == "content_filter"
    assert perr.ContextWindowError("x").category == "context_window"
    assert perr.RateLimitError("x").category == "rate_limit"
    assert perr.NetworkError("x").category == "network"


def test_provider_error_carries_detail() -> None:
    exc = perr.AuthError("invalid key", detail={"request_id": "req_x"})
    assert exc.detail == {"request_id": "req_x"}
    assert str(exc) == "invalid key"


def test_provider_error_default_detail_is_empty_dict() -> None:
    exc = perr.NetworkError("conn refused")
    assert exc.detail == {}


def test_make_error_event_default_kind_is_class_name() -> None:
    ev = perr.make_error_event(perr.RateLimitError("429"))
    assert ev["type"] == "error"
    p = ev["payload"]
    assert p["category"] == "rate_limit"
    assert p["kind"] == "RateLimitError"
    assert p["message"] == "429"
    assert p["detail"] == {}


def test_make_error_event_explicit_kind_wins() -> None:
    ev = perr.make_error_event(
        perr.ConfigError("transcript ended dangling"),
        kind="transcript_invalid",
    )
    assert ev["payload"]["kind"] == "transcript_invalid"
    assert ev["payload"]["category"] == "config"


def test_make_unknown_error_event_shape() -> None:
    ev = perr.make_unknown_error_event(RuntimeError("ka-boom"))
    assert ev["type"] == "error"
    p = ev["payload"]
    assert p["category"] == "unknown"
    assert p["kind"] == "RuntimeError"
    assert "ka-boom" in p["message"]
    assert "unclassified" in p["message"]
    assert p["detail"] == {}
