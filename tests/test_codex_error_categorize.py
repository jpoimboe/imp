
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


def test_unknown_error_falls_back_to_api() -> None:
    err = categorize_turn_failed({"status": 418, "message": "I'm a teapot"})
    assert isinstance(err, perr.ApiError)
