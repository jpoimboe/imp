# tests/test_tool_registry.py
from __future__ import annotations

import pytest

from imp.agents.tools import (
    Tool,
    _REGISTRY,
    get_registry,
    imp_tool,
)


@pytest.fixture
def isolated_registry():
    """Snapshot the global registry, clear it for the test, restore on teardown.

    Tests in this file exercise the @imp_tool decorator directly, which mutates
    the global _REGISTRY. Without this fixture, tests in other files that rely
    on built-ins being present (test_fs_tools.py, etc.) break when their test
    order interleaves with these.
    """
    snapshot = dict(_REGISTRY)
    _REGISTRY.clear()
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


def test_register_and_lookup(isolated_registry) -> None:
    @imp_tool(name="my_echo", description="Echo the input back.")
    def echo(ctx: object, message: str) -> str:
        return message

    reg = get_registry()
    assert "my_echo" in reg
    t = reg["my_echo"]
    assert isinstance(t, Tool)
    assert t.name == "my_echo"
    assert t.description == "Echo the input back."


def test_schema_derived_from_type_hints(isolated_registry) -> None:
    @imp_tool(name="adder", description="Add two ints.")
    def add(ctx: object, a: int, b: int) -> int:
        return a + b

    schema = get_registry()["adder"].input_schema
    assert schema["type"] == "object"
    assert schema["properties"]["a"]["type"] == "integer"
    assert schema["properties"]["b"]["type"] == "integer"
    assert set(schema["required"]) == {"a", "b"}


def test_invoke_calls_wrapped_function(isolated_registry) -> None:
    @imp_tool(name="incr", description="Increment by amount.")
    def incr(ctx: object, n: int, by: int = 1) -> int:
        return n + by

    out = get_registry()["incr"].invoke(ctx=None, args={"n": 5, "by": 3})
    assert out == 8


def test_registry_restored_after_fixture(isolated_registry) -> None:
    """Sanity: in-fixture mutations don't leak — verified by the fixture
    snapshot/restore; this test just registers something to exercise that path."""
    @imp_tool(name="ephemeral", description="x")
    def _ephemeral(ctx: object) -> str:
        return "1"

    assert "ephemeral" in get_registry()
    # After this test exits, the fixture's teardown restores the prior snapshot,
    # so tests in other files don't see "ephemeral" or any clear-related fallout.
