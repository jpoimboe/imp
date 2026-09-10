
import pytest

from imp.providers import errors as perr
from imp.providers.codex_runner import categorize_turn_failed


def test_chatgpt_account_400_maps_to_config() -> None:
    err = categorize_turn_failed({
        "status": 400,
        "message": "model gpt-5.5-codex is not supported with a ChatGPT account",
    })
    assert isinstance(err, perr.ConfigError)


def test_401_maps_to_auth() -> None:
    assert isinstance(categorize_turn_failed({"status": 401,
                                              "message": "Unauthorized"}),
                      perr.AuthError)


def test_subprocess_network_timeout_maps_to_network() -> None:
    err = categorize_turn_failed({
        "kind": "network", "message": "subprocess timeout after 30s",
    })
    assert isinstance(err, perr.NetworkError)


CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."


@pytest.mark.parametrize("message", [
    CAPACITY_MESSAGE,
    "Request failed: SELECTED MODEL IS AT CAPACITY. PLEASE TRY A DIFFERENT MODEL. Retry later.",
])
def test_model_capacity_maps_to_network(message: str) -> None:
    err = categorize_turn_failed({"message": message})
    assert isinstance(err, perr.NetworkError)
    assert err.category == "network"
    assert str(err) == f"codex: {message}"


@pytest.mark.parametrize(("error", "category"), [
    ({"status": 401, "message": f"Unauthorized: {CAPACITY_MESSAGE}"}, "auth"),
    ({"status": 400, "message": "unsupported ChatGPT account/model config; "
                                f"{CAPACITY_MESSAGE}"}, "config"),
    ({"message": f"context window exceeded; {CAPACITY_MESSAGE}"}, "context_window"),
])
def test_model_capacity_does_not_override_earlier_categories(
    error: dict[str, object], category: str,
) -> None:
    assert categorize_turn_failed(error).category == category


def test_unknown_error_falls_back_to_api() -> None:
    err = categorize_turn_failed({"status": 418, "message": "I'm a teapot"})
    assert isinstance(err, perr.ApiError)


def test_refresh_token_reuse_maps_to_auth() -> None:
    err = categorize_turn_failed({
        "message": "refresh token was already used. "
                   "Please log out and sign in again.",
    })
    assert isinstance(err, perr.AuthError)
    assert err.args[0] == (
        "codex: refresh token was already used. "
        "Please log out and sign in again."
    )


def test_refresh_reuse_partial_signature_maps_to_auth() -> None:
    err = categorize_turn_failed({"message": "refresh token was already used."})
    assert isinstance(err, perr.AuthError)


@pytest.mark.parametrize("message", [
    "context_length_exceeded",
    "This model's maximum context length is 258400 tokens",
    "the request exceeds the context window for this model",
    "too many tokens in the request",
])
def test_context_overflow_messages_map_to_context_window(message: str) -> None:
    err = categorize_turn_failed({"status": 400, "message": message})
    assert isinstance(err, perr.ContextWindowError)
    assert err.category == "context_window"


def test_context_overflow_beats_the_network_branch() -> None:
    # "timeout" alone routes to network; an overflow message carrying it must
    # not, or Tier B reincarnation never fires and imp just retries forever.
    err = categorize_turn_failed({
        "message": "context_length_exceeded (connection timeout during retry)",
    })
    assert err.category == "context_window"


def test_auth_still_beats_the_context_window_branch() -> None:
    err = categorize_turn_failed({
        "status": 401,
        "message": "Unauthorized: maximum context length",
    })
    assert err.category == "auth"


def test_chatgpt_account_400_still_beats_the_context_window_branch() -> None:
    err = categorize_turn_failed({
        "status": 400,
        "message": "model gpt-5.6-sol is not supported with a ChatGPT account; "
                   "context window mismatch",
    })
    assert err.category == "config"


class _OpaqueError:
    """The shape where codex hands `turn.failed.error` an object, not a dict."""

    def __str__(self) -> str:
        return ("Your access token could not be refreshed because your refresh "
                "token was already used. Please log out and sign in again.")


def test_a_non_dict_reuse_error_is_still_classified_auth() -> None:
    err = categorize_turn_failed(_OpaqueError())
    assert err.category == "auth"
    assert "already used" in str(err)


def test_a_non_dict_error_with_no_signature_falls_back_to_api_with_the_prefix() -> None:
    err = categorize_turn_failed("something went wrong")
    assert isinstance(err, perr.ApiError)
    assert str(err) == "codex: something went wrong"
