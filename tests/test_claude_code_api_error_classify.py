"""Fix G: claude CLI API-error text is classified, not surfaced as a turn."""

from __future__ import annotations

from imp.providers import errors as perr
from imp.providers.claude_code import _classify_agent_text


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


def test_four_digit_code_not_misclassified() -> None:
    # "API Error: 4001" must NOT be read as a 4xx (no grabbing the first 3 digits).
    assert _classify_agent_text("API Error: 4001 weird") is None


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


def test_codeless_phrase_same_line_still_network() -> None:
    # Boundary: phrase on the SAME line as the API Error: prefix still classifies.
    assert isinstance(_classify_agent_text("API Error: Overloaded"), perr.NetworkError)
    assert isinstance(
        _classify_agent_text("API Error: Internal server error"), perr.NetworkError)


def test_codeless_phrase_on_different_line_is_none() -> None:
    # Regression: an API Error: line with an unrelated body, plus the transient
    # word only in prose on a DIFFERENT line, must NOT classify — it pauses.
    multiline = (
        "API Error: the request did not complete\n"
        "Earlier the build server was Overloaded with traffic."
    )
    assert _classify_agent_text(multiline) is None
    multiline2 = (
        "API Error: connection reset by peer\n"
        "An Internal server error happened to a coworker last week."
    )
    assert _classify_agent_text(multiline2) is None
