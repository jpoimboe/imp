"""Fix G: claude CLI API-error text is classified, not surfaced as a turn."""

from __future__ import annotations

from imp.providers import errors as perr
from imp.providers.claude_code import _classify_agent_text, _termination_from_result
from tests.cc_fakes import result


def test_400_classified_as_api_error_with_http_kind() -> None:
    exc = _classify_agent_text("API Error: 400 messages.98.content.0... is_error")
    assert isinstance(exc, perr.ApiError)
    assert exc.category == "api"
    assert getattr(exc, "kind", "") == "http_400"


def test_404_classified_as_api_error() -> None:
    exc = _classify_agent_text("API Error: 404 not found")
    assert isinstance(exc, perr.ApiError)
    assert getattr(exc, "kind", "") == "http_404"


def test_429_classified_as_rate_limit_not_api() -> None:
    exc = _classify_agent_text("API Error: 429 rate limited")
    assert isinstance(exc, perr.RateLimitError)
    assert not isinstance(exc, perr.ApiError)


def test_5xx_classified_as_network() -> None:
    # §10: 5xx → NetworkError (network category → RetryWithBackoff). Previously
    # returned None (treated as agent text → wrong nudge loop).
    from imp.providers import errors as perr
    from imp.providers.claude_code import _classify_agent_text
    err = _classify_agent_text("API Error: 529 Overloaded")
    assert isinstance(err, perr.NetworkError)


def test_500_classified_as_network() -> None:
    assert isinstance(_classify_agent_text("API Error: 500 server error"), perr.NetworkError)


def test_auth_pattern_still_wins() -> None:
    assert isinstance(_classify_agent_text("Invalid API key"), perr.AuthError)


def test_normal_text_unclassified() -> None:
    assert _classify_agent_text("I edited bash.py and ran the tests.") is None


def test_make_error_event_carries_api_kind() -> None:
    exc = _classify_agent_text("API Error: 400 bad")
    ev = perr.make_error_event(exc, kind=getattr(exc, "kind", ""))
    assert ev["payload"]["category"] == "api"
    assert ev["payload"]["kind"] == "http_400"


def test_four_digit_code_is_statusless_network() -> None:
    # "API Error: 4001" exposes no routable 3-digit HTTP code (the regex's
    # negative lookahead rejects it), so it is statusless transport -> network.
    assert isinstance(
        _classify_agent_text("API Error: 4001 weird"), perr.NetworkError)


def test_3xx_code_not_classified() -> None:
    # 3xx falls through both the 429 and 4xx branches -> None.
    assert _classify_agent_text("API Error: 301 moved") is None


def test_codeless_internal_server_error_is_network() -> None:
    err = _classify_agent_text("API Error: Internal server error")
    assert isinstance(err, perr.NetworkError)


def test_codeless_overloaded_is_network() -> None:
    err = _classify_agent_text("API Error: Overloaded")
    assert isinstance(err, perr.NetworkError)


def test_codeless_phrase_without_api_error_prefix_is_none() -> None:
    # The same words in ordinary prose (no `API Error:` anchor) must NOT classify.
    assert _classify_agent_text("Internal server error: retrying the build now") is None
    assert _classify_agent_text("The job was Overloaded with work, so I split it") is None


def test_auth_still_wins_over_codeless_phrase() -> None:
    # Auth patterns are checked first even when an API Error transient phrase co-occurs.
    err = _classify_agent_text("Invalid API key — API Error: Overloaded")
    assert isinstance(err, perr.AuthError)


def test_quota_still_wins_over_codeless_phrase() -> None:
    # Quota patterns are checked first, same as auth — they win even when a
    # code-less transient phrase co-occurs.
    err = _classify_agent_text("Credit balance is too low — API Error: Overloaded")
    assert isinstance(err, perr.QuotaError)


def test_codeless_multiline_api_error_is_network() -> None:
    # Broad transport stance: a statusless `API Error:` line is transient
    # regardless of where any other words sit. (Old per-line matching paused
    # these; the catch-all now retries them.)
    multiline = (
        "API Error: the request did not complete\n"
        "Earlier the build server was Overloaded with traffic."
    )
    assert isinstance(_classify_agent_text(multiline), perr.NetworkError)
    multiline2 = (
        "API Error: connection reset by peer\n"
        "An Internal server error happened to a coworker last week."
    )
    assert isinstance(_classify_agent_text(multiline2), perr.NetworkError)


def test_invalid_authentication_credentials_phrase_is_auth() -> None:
    exc = _classify_agent_text("Invalid authentication credentials")
    assert isinstance(exc, perr.AuthError)


def test_failed_to_authenticate_phrase_is_auth() -> None:
    exc = _classify_agent_text("Failed to authenticate")
    assert isinstance(exc, perr.AuthError)


def test_lowercase_failed_to_authenticate_prose_is_not_auth() -> None:
    assert _classify_agent_text(
        "the script failed to authenticate against the mock server") is None


def test_401_code_classified_as_auth_with_http_kind() -> None:
    exc = _classify_agent_text("API Error: 401 rejected by gateway")
    assert isinstance(exc, perr.AuthError)
    assert getattr(exc, "kind", "") == "http_401"


def test_403_code_classified_as_auth_with_http_kind() -> None:
    exc = _classify_agent_text("API Error: 403 forbidden")
    assert isinstance(exc, perr.AuthError)
    assert getattr(exc, "kind", "") == "http_403"


def test_408_code_classified_as_network() -> None:
    exc = _classify_agent_text("API Error: 408 request timeout")
    assert isinstance(exc, perr.NetworkError)


def test_422_code_still_api_error() -> None:
    exc = _classify_agent_text("API Error: 422 unprocessable")
    assert isinstance(exc, perr.ApiError)
    assert getattr(exc, "kind", "") == "http_422"


def test_observed_401_credential_string_is_auth() -> None:
    exc = _classify_agent_text(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials")
    assert isinstance(exc, perr.AuthError)


def test_connection_refused_codeless_is_network() -> None:
    # The exact eo5ehc shape: a transport failure carrying no HTTP code.
    err = _classify_agent_text(
        "API Error: Unable to connect to API (ConnectionRefused)")
    assert isinstance(err, perr.NetworkError)


def test_termination_connection_refused_is_network_not_terminated() -> None:
    # The eo5ehc regression at the _termination_from_result boundary: an
    # is_error result whose text is a statusless transport failure must come
    # back NetworkError (-> RetryWithBackoff), never TerminatedError (-> Pause).
    from types import SimpleNamespace

    from imp.providers.claude_code import _termination_from_result
    sdk_msg = SimpleNamespace(
        is_error=True, subtype="success", api_error_status=None,
        stop_reason=None, errors=None)
    out = _termination_from_result(
        sdk_msg, "API Error: Unable to connect to API (ConnectionRefused)", {})
    assert isinstance(out, perr.NetworkError)
    assert not isinstance(out, perr.TerminatedError)


def test_transient_phrases_constant_removed() -> None:
    import imp.providers.claude_code as cc
    assert not hasattr(cc, "_TRANSIENT_PHRASES")


def test_429_status_result_classifies_rate_limit() -> None:
    # last_text carries no API-error/session-limit wording, so _classify_agent_text
    # returns None and only the api_error_status==429 branch can classify it.
    sdk = result(is_error=True, api_error_status=429)
    term = _termination_from_result(sdk, "some unrelated agent prose", {})
    assert isinstance(term, perr.RateLimitError)
    assert not isinstance(term, perr.TerminatedError)
    assert term.detail["api_error_status"] == 429


def test_session_limit_text_classifies_rate_limit() -> None:
    exc = _classify_agent_text(
        "You've hit your session limit · resets 11:30pm (America/Los_Angeles)")
    assert isinstance(exc, perr.RateLimitError)


def test_session_limit_phrase_classifies_rate_limit() -> None:
    assert isinstance(
        _classify_agent_text("You have hit a session limit."), perr.RateLimitError)


def test_ordinary_prose_stays_unclassified_after_backstop() -> None:
    assert _classify_agent_text("Reading the spec and editing files.") is None


def test_scheduling_prose_with_timezone_stays_unclassified() -> None:
    assert _classify_agent_text(
        "The cron job resets daily at midnight (America/Chicago) every Sunday") is None
    assert _classify_agent_text(
        "Database resets at 3am (America/New_York) each week") is None
