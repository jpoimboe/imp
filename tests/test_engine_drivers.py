from imp.engine.drivers import (
    DRIVER_COMMANDS,
    KNOWN_DRIVERS,
    driver_allowed_tools,
    driver_stdio_servers,
)


def test_impty_is_a_known_stdio_driver() -> None:
    assert frozenset({"impty"}) == KNOWN_DRIVERS
    assert DRIVER_COMMANDS["impty"] == ["python", "-m", "impty.mcp"]


def test_allowed_tools_emits_sorted_wildcards() -> None:
    assert driver_allowed_tools({"impty"}) == ["mcp__impty__*"]
    assert driver_allowed_tools(frozenset()) == []
    assert driver_allowed_tools(["zebra", "alpha"]) == ["mcp__alpha__*", "mcp__zebra__*"]


def test_stdio_servers_emit_sdk_stdio_config() -> None:
    assert driver_stdio_servers({"impty"}) == {
        "impty": {"type": "stdio", "command": "python", "args": ["-m", "impty.mcp"]}
    }
    assert driver_stdio_servers(frozenset()) == {}
