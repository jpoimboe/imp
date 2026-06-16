from imp.engine.drivers import KNOWN_DRIVERS, driver_allowed_tools


def test_impty_is_the_known_driver() -> None:
    assert KNOWN_DRIVERS == frozenset({"impty"})


def test_allowed_tools_emits_sorted_wildcards() -> None:
    assert driver_allowed_tools({"impty"}) == ["mcp__impty__*"]
    assert driver_allowed_tools(frozenset()) == []
    assert driver_allowed_tools(["zebra", "alpha"]) == ["mcp__alpha__*", "mcp__zebra__*"]


def test_stdio_driver_wiring_is_gone() -> None:
    import imp.engine.drivers as drivers

    assert not hasattr(drivers, "DRIVER_COMMANDS")
    assert not hasattr(drivers, "driver_stdio_servers")
