"""Fix G: claude CLI API-error text is classified, not surfaced as a turn."""

from __future__ import annotations

from typing import Any

from imp.providers import errors as perr
from imp.providers.claude_code import _classify_agent_text, _termination_from_result
from tests.cc_fakes import result


def _classified(text: str, **kw: Any) -> perr.ProviderError | None:
    """These tests pin wording→category routing; the chrome evidence the anchored
    gate now requires is a given."""
    return _classify_agent_text(text, output_tokens=0, **kw)


def test_400_classified_as_api_error_with_http_kind() -> None:
    exc = _classified("API Error: 400 messages.98.content.0... is_error")
    assert isinstance(exc, perr.ApiError)
    assert exc.category == "api"
    assert getattr(exc, "kind", "") == "http_400"


def test_404_classified_as_api_error() -> None:
    exc = _classified("API Error: 404 not found")
    assert isinstance(exc, perr.ApiError)
    assert getattr(exc, "kind", "") == "http_404"


def test_429_classified_as_rate_limit_not_api() -> None:
    exc = _classified("API Error: 429 rate limited")
    assert isinstance(exc, perr.RateLimitError)
    assert not isinstance(exc, perr.ApiError)


def test_5xx_classified_as_network() -> None:
    # §10: 5xx → NetworkError (network category → RetryWithBackoff). Previously
    # returned None (treated as agent text → wrong nudge loop).
    from imp.providers import errors as perr
    err = _classified("API Error: 529 Overloaded")
    assert isinstance(err, perr.NetworkError)


def test_500_classified_as_network() -> None:
    assert isinstance(_classified("API Error: 500 server error"), perr.NetworkError)


def test_auth_pattern_still_wins() -> None:
    assert isinstance(_classified("Invalid API key"), perr.AuthTerminalError)


def test_normal_text_unclassified() -> None:
    assert _classified("I edited bash.py and ran the tests.") is None


def test_make_error_event_carries_api_kind() -> None:
    exc = _classified("API Error: 400 bad")
    ev = perr.make_error_event(exc, kind=getattr(exc, "kind", ""))
    assert ev["payload"]["category"] == "api"
    assert ev["payload"]["kind"] == "http_400"


def test_four_digit_code_is_statusless_network() -> None:
    # "API Error: 4001" exposes no routable 3-digit HTTP code (the regex's
    # negative lookahead rejects it), so it is statusless transport -> network.
    assert isinstance(
        _classified("API Error: 4001 weird"), perr.NetworkError)


def test_3xx_code_not_classified() -> None:
    # 3xx falls through both the 429 and 4xx branches -> None.
    assert _classified("API Error: 301 moved") is None


def test_codeless_internal_server_error_is_network() -> None:
    err = _classified("API Error: Internal server error")
    assert isinstance(err, perr.NetworkError)


def test_codeless_overloaded_is_network() -> None:
    err = _classified("API Error: Overloaded")
    assert isinstance(err, perr.NetworkError)


def test_codeless_phrase_without_api_error_prefix_is_none() -> None:
    # The same words in ordinary prose (no `API Error:` anchor) must NOT classify.
    assert _classified("Internal server error: retrying the build now") is None
    assert _classified("The job was Overloaded with work, so I split it") is None


def test_auth_still_wins_over_codeless_phrase() -> None:
    # Auth patterns are checked first even when an API Error transient phrase co-occurs.
    err = _classified("Invalid API key — API Error: Overloaded")
    assert isinstance(err, perr.AuthTerminalError)


def test_quota_still_wins_over_codeless_phrase() -> None:
    # Quota patterns are checked first, same as auth — they win even when a
    # code-less transient phrase co-occurs.
    err = _classified("Credit balance is too low — API Error: Overloaded")
    assert isinstance(err, perr.QuotaError)


def test_codeless_multiline_api_error_is_network() -> None:
    # Broad transport stance: a statusless `API Error:` line is transient
    # regardless of where any other words sit. (Old per-line matching paused
    # these; the catch-all now retries them.)
    multiline = (
        "API Error: the request did not complete\n"
        "Earlier the build server was Overloaded with traffic."
    )
    assert isinstance(_classified(multiline), perr.NetworkError)
    multiline2 = (
        "API Error: connection reset by peer\n"
        "An Internal server error happened to a coworker last week."
    )
    assert isinstance(_classified(multiline2), perr.NetworkError)


def test_invalid_authentication_credentials_phrase_is_terminal_auth() -> None:
    exc = _classified("Invalid authentication credentials")
    assert isinstance(exc, perr.AuthTerminalError)


def test_failed_to_authenticate_phrase_is_terminal_auth() -> None:
    exc = _classified("Failed to authenticate")
    assert isinstance(exc, perr.AuthTerminalError)


def test_lowercase_failed_to_authenticate_prose_is_not_auth() -> None:
    assert _classified(
        "the script failed to authenticate against the mock server") is None


def test_401_code_classified_as_auth_with_http_kind() -> None:
    exc = _classified("API Error: 401 rejected by gateway")
    assert isinstance(exc, perr.AuthError)
    assert getattr(exc, "kind", "") == "http_401"


def test_403_code_classified_as_auth_with_http_kind() -> None:
    exc = _classified("API Error: 403 forbidden")
    assert isinstance(exc, perr.AuthError)
    assert getattr(exc, "kind", "") == "http_403"


def test_408_code_classified_as_network() -> None:
    exc = _classified("API Error: 408 request timeout")
    assert isinstance(exc, perr.NetworkError)


def test_422_code_still_api_error() -> None:
    exc = _classified("API Error: 422 unprocessable")
    assert isinstance(exc, perr.ApiError)
    assert getattr(exc, "kind", "") == "http_422"


def test_observed_401_credential_string_routes_on_the_status_code() -> None:
    exc = _classified(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials")
    assert isinstance(exc, perr.AuthError)
    assert not isinstance(exc, perr.AuthTerminalError)
    assert exc.category == "auth"
    assert getattr(exc, "kind", "") == "http_401"


def test_revoked_oauth_token_prose_is_retryable_auth() -> None:
    # The observed regression string, verbatim from the claude CLI.
    exc = _classified(
        "Failed to authenticate. API Error: 401 OAuth access token has been revoked.")
    assert isinstance(exc, perr.AuthError)
    assert not isinstance(exc, perr.AuthTerminalError)
    assert exc.category == "auth"
    assert getattr(exc, "kind", "") == "http_401"


def test_connection_refused_codeless_is_network() -> None:
    # The exact eo5ehc shape: a transport failure carrying no HTTP code.
    err = _classified(
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
    exc = _classified(
        "You've hit your session limit · resets 11:30pm (America/Los_Angeles)")
    assert isinstance(exc, perr.RateLimitError)


def test_session_limit_phrase_classifies_rate_limit() -> None:
    assert isinstance(
        _classified("You have hit a session limit."), perr.RateLimitError)


def test_weekly_limit_phrase_classifies_rate_limit() -> None:
    assert isinstance(
        _classified("You have hit a weekly limit."), perr.RateLimitError)


def test_ordinary_prose_stays_unclassified_after_backstop() -> None:
    assert _classified("Reading the spec and editing files.") is None


def test_scheduling_prose_with_timezone_stays_unclassified() -> None:
    assert _classified(
        "The cron job resets daily at midnight (America/Chicago) every Sunday") is None
    assert _classified(
        "Database resets at 3am (America/New_York) each week") is None


def test_session_limit_prose_stays_unclassified() -> None:
    assert _classified("The session limit is set to 1000 seconds.") is None
    assert _classified("I'm working on session limit handling.") is None


def test_400_tool_use_concurrency_emdash_classifies_network_not_terminated() -> None:
    # The real mehu87 termination: a 400 whose agent text says "tool use
    # concurrency" with no colon, so _classify_agent_text returns None. It must
    # classify transient (NetworkError → RetryWithBackoff), never TerminatedError.
    assert _classified("API Error 400 — tool use concurrency") is None
    sdk = result(is_error=True, api_error_status=400)
    term = _termination_from_result(
        sdk, "API Error 400 — tool use concurrency", {})
    assert isinstance(term, perr.NetworkError)
    assert not isinstance(term, perr.TerminatedError)
    assert term.category == "network"
    assert term.detail["api_error_status"] == 400


def test_pattern_quoted_past_first_line_is_not_classified() -> None:
    # The redu99 regression: a subagent report *about* the auth patterns quotes
    # them mid-document. Classification is anchored to the first line, so a
    # prose opener means no match — the report must survive as agent output.
    report = (
        "Here are the verified findings, all cited to exact line numbers:\n"
        "\n"
        "## 1. `_AUTH_PATTERNS` contents\n"
        "- `Invalid API key`\n"
        "- `Please run `/login``\n"
        "- a colon-form example: API Error: 400 bad request\n"
        "- quota: You have reached your usage limit\n"
    )
    assert _classified(report) is None


def test_whole_message_error_with_situation_prefix_still_auth() -> None:
    # Genuine CLI shape: `<situation> · <remedy>` as the entire message (mona44
    # was `Not logged in · Please run /login`). The fragment sits mid-line, so
    # first-line anchoring must not require a match at offset 0.
    exc = _classified("Invalid API key \u00b7 Please run `/login`")
    assert isinstance(exc, perr.AuthTerminalError)


def test_first_line_error_with_trailing_lines_still_classified() -> None:
    err = _classified(
        "API Error: 529 Overloaded\nThe request can be retried shortly.")
    assert isinstance(err, perr.NetworkError)


def test_leading_blank_lines_before_error_still_classified() -> None:
    exc = _classified("\n\n  Invalid API key \u00b7 Please run `/login`")
    assert isinstance(exc, perr.AuthTerminalError)


def test_unanchored_mode_matches_past_first_line() -> None:
    # anchored=False keeps the historical full-text scan for callers that
    # already hold an out-of-band error signal (_termination_from_result).
    text = "the turn ended abnormally\nYou have reached your usage limit"
    assert _classified(text) is None
    assert isinstance(
        _classified(text, anchored=False), perr.QuotaError)


def test_termination_labels_pattern_past_first_line() -> None:
    # _termination_from_result is gated on is_error, so its substring labeler
    # scans the full text: a quota phrase past line one must still label.
    sdk = result(is_error=True, api_error_status=None)
    term = _termination_from_result(
        sdk, "the CLI reported:\nYou have reached your usage limit", {})
    assert isinstance(term, perr.QuotaError)


def test_each_auth_pattern_classifies_terminal() -> None:
    for prose in ("Invalid API key", "Please run `/login`", "Please run /login",
                  "Invalid authentication credentials", "Failed to authenticate"):
        exc = _classified(prose)
        assert isinstance(exc, perr.AuthTerminalError), prose
        assert exc.category == "auth_terminal"


def test_status_code_wins_over_the_prose() -> None:
    # A routable code proves the request reached the server, so the mixed
    # string is a server rejection, not a local credential-clear.
    exc = _classified(
        "Please run /login · API Error: 401 Invalid authentication credentials")
    assert isinstance(exc, perr.AuthError)
    assert not isinstance(exc, perr.AuthTerminalError)
    assert exc.category == "auth"
    assert getattr(exc, "kind", "") == "http_401"


def test_each_auth_pattern_routes_on_a_routable_code() -> None:
    for prose in ("Invalid API key", "Please run `/login`", "Please run /login",
                  "Invalid authentication credentials", "Failed to authenticate"):
        exc = _classified(f"{prose} · API Error: 401 rejected")
        assert isinstance(exc, perr.AuthError), prose
        assert not isinstance(exc, perr.AuthTerminalError), prose
        assert exc.category == "auth", prose


def test_auth_prose_with_a_5xx_routes_to_network() -> None:
    exc = _classified("Failed to authenticate. API Error: 503 upstream down")
    assert isinstance(exc, perr.NetworkError)
    assert exc.category == "network"


def test_auth_prose_with_an_unrouted_status_stays_terminal() -> None:
    # 302 matches the regex but hits no routing branch. Yielding on a bare
    # match would drop the text past every branch and return None.
    exc = _classified("Failed to authenticate. API Error: 302 redirected")
    assert isinstance(exc, perr.AuthTerminalError)
    assert exc.category == "auth_terminal"


def test_the_discriminator_applies_unanchored_too() -> None:
    # The _termination_from_result seam scans the whole text, not the first line.
    mixed = _classified(
        "Session ended abnormally.\n"
        "Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
        anchored=False)
    assert isinstance(mixed, perr.AuthError)
    assert mixed.category == "auth"
    prose_only = _classified(
        "Session ended abnormally.\nNot logged in · Please run /login",
        anchored=False)
    assert isinstance(prose_only, perr.AuthTerminalError)
    assert prose_only.category == "auth_terminal"


def test_bare_status_auth_stays_transient() -> None:
    for code in (401, 403):
        exc = _classified(f"API Error: {code} rejected by gateway")
        assert isinstance(exc, perr.AuthError)
        assert not isinstance(exc, perr.AuthTerminalError)
        assert exc.category == "auth"
        assert getattr(exc, "kind", "") == f"http_{code}"


def test_terminal_prose_message_carries_the_text_not_the_pattern() -> None:
    exc = _classified("Not logged in · Please run /login")
    assert isinstance(exc, perr.AuthTerminalError)
    assert str(exc) == "claude CLI: Not logged in · Please run /login"


def test_terminal_prose_message_truncates_at_200_chars() -> None:
    long_text = "Please run /login " + "x" * 400
    exc = _classified(long_text)
    assert isinstance(exc, perr.AuthTerminalError)
    assert str(exc) == f"claude CLI: {long_text[:200]}"
    assert len(str(exc)) == len("claude CLI: ") + 200


def test_400_tool_use_concurrency_colon_form_wins_over_api_error_label() -> None:
    # Colon form: _classify_agent_text labels this a terminal ApiError(http_400).
    # The 400/concurrency rule must override that classification → NetworkError.
    labeled = _classified("API Error: 400 messages: tool use concurrency")
    assert isinstance(labeled, perr.ApiError)
    assert labeled.category == "api"
    sdk = result(is_error=True, api_error_status=400)
    term = _termination_from_result(
        sdk, "API Error: 400 messages: tool use concurrency", {})
    assert term.category == "network"
    assert not isinstance(term, perr.TerminatedError)
