from imp.evals.models import Telemetry


def test_telemetry_carries_auth_mode_field() -> None:
    t = Telemetry.zero()
    # Default is None: non-codex providers omit it; consumers read None as
    # "not applicable / real spend".
    assert t.auth_mode is None

    t2 = Telemetry(
        input_tokens=1, output_tokens=2, cache_read_tokens=3,
        cache_creation_tokens=4, tool_calls=5, wall_seconds=6.0,
        auth_mode="chatgpt",
    )
    assert t2.auth_mode == "chatgpt"

    # Round-trips through to_dict/from_dict (this is the persisted shape that
    # downstream eval consumers actually read).
    d = t2.to_dict()
    assert d["auth_mode"] == "chatgpt"
    assert Telemetry.from_dict(d).auth_mode == "chatgpt"

    # Backward shape (no auth_mode key) defaults to None.
    legacy = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
              "cache_creation_tokens": 0, "tool_calls": 0, "wall_seconds": 0.0}
    assert Telemetry.from_dict(legacy).auth_mode is None
