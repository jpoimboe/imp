from __future__ import annotations

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


def test_make_unknown_error_event_attaches_detail() -> None:
    ev = perr.make_unknown_error_event(
        RuntimeError("boom"), detail={"stderr_tail": "fatal: nope"})
    assert ev["payload"]["category"] == "unknown"
    assert ev["payload"]["detail"] == {"stderr_tail": "fatal: nope"}


def test_make_unknown_error_event_detail_defaults_empty() -> None:
    ev = perr.make_unknown_error_event(RuntimeError("boom"))
    assert ev["payload"]["detail"] == {}


def test_terminated_error_is_first_class_pause_category() -> None:
    from typing import get_args

    from imp.engine import error_policy as ep

    err = perr.TerminatedError("agent terminated", detail={"is_error": True})
    assert err.category == "terminated"
    assert err.detail == {"is_error": True}
    # Distinct, Pause-policy, and covered by the policy table.
    assert "terminated" in get_args(perr.Category)
    assert isinstance(ep.decide("terminated", prior_backoff_attempts=0), ep.Pause)
    event = perr.make_error_event(err, kind="terminated")
    assert event["payload"]["category"] == "terminated"
    assert event["payload"]["detail"] == {"is_error": True}


def test_auth_terminal_is_a_sibling_of_auth_not_a_subclass() -> None:
    exc = perr.AuthTerminalError("claude: OAuth refresh token is dead")
    assert exc.category == "auth_terminal"
    assert isinstance(exc, perr.ProviderError)
    # A sibling, never a subclass: an existing isinstance(..., AuthError) guard
    # must not silently swallow the terminal case.
    assert not isinstance(exc, perr.AuthError)


def test_auth_terminal_error_event_kind_defaults_to_class_name() -> None:
    ev = perr.make_error_event(perr.AuthTerminalError("dead login"))
    assert ev["payload"]["category"] == "auth_terminal"
    assert ev["payload"]["kind"] == "AuthTerminalError"


def _raise_messageless() -> None:
    raise AssertionError


def _ping(n: int) -> None:
    if n == 0:
        raise RuntimeError("deep")
    _pong(n - 1)


def _pong(n: int) -> None:
    _ping(n - 1)


def test_make_unknown_error_event_messageless_names_crash_site() -> None:
    supplied: dict[str, object] = {"stderr_tail": "fatal: nope"}
    try:
        _raise_messageless()
    except AssertionError as exc:
        ev = perr.make_unknown_error_event(exc, detail=supplied)
    raise_line = _raise_messageless.__code__.co_firstlineno + 1  # the `raise` line
    p = ev["payload"]
    assert p["category"] == "unknown"
    assert p["kind"] == "AssertionError"
    assert p["message"] == (
        f"unclassified provider error: AssertionError at test_provider_errors.py:{raise_line}")
    assert p["detail"]["stderr_tail"] == "fatal: nope"
    assert "_raise_messageless" in str(p["detail"]["traceback_tail"])
    assert supplied == {"stderr_tail": "fatal: nope"}


def test_make_unknown_error_event_traceback_tail_keeps_last_15_lines() -> None:
    try:
        _ping(40)
    except RuntimeError as exc:
        ev = perr.make_unknown_error_event(exc)
    p = ev["payload"]
    assert p["message"] == "unclassified provider error: deep"
    tail = str(p["detail"]["traceback_tail"]).splitlines()
    assert len(tail) == 15
    assert tail[-1] == "RuntimeError: deep"


def test_make_unknown_error_event_without_traceback_names_the_type() -> None:
    ev = perr.make_unknown_error_event(AssertionError())
    p = ev["payload"]
    assert p["message"] == "unclassified provider error: AssertionError"
    assert p["detail"] == {}
