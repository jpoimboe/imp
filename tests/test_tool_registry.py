# tests/test_tool_registry.py
from __future__ import annotations

from typing import Literal

import pytest

from imp.agents.tools import (
    _REGISTRY,
    Tool,
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


def test_literal_annotation_exports_enum_under_pep563(isolated_registry) -> None:
    # This module has `from __future__ import annotations`, so the annotation
    # below reaches _build_input_schema as the STRING 'Literal["a", "b"]' —
    # the production shape of nav.py/fs.py tools. Today this raises
    # PydanticUserError ("not fully defined") at decoration time.
    @imp_tool(name="modal", description="x")
    def modal(ctx: object, mode: Literal["a", "b"] = "a") -> str:
        return mode

    props = get_registry()["modal"].input_schema["properties"]
    assert props["mode"]["enum"] == ["a", "b"]


def test_unannotated_param_defaults_to_str(isolated_registry) -> None:
    # get_type_hints OMITS unannotated params; the resolver must keep today's
    # unannotated -> str behavior, not silently change schema shape.
    @imp_tool(name="untyped", description="x")
    def untyped(ctx: object, q) -> str:  # q deliberately unannotated
        return str(q)

    props = get_registry()["untyped"].input_schema["properties"]
    assert props["q"]["type"] == "string"


def test_return_annotation_not_in_schema(isolated_registry) -> None:
    # get_type_hints returns a "return" key; it must never become a property.
    @imp_tool(name="ret", description="x")
    def ret(ctx: object, a: int) -> int:
        return a

    props = get_registry()["ret"].input_schema["properties"]
    assert set(props) == {"a"}


def test_unresolvable_annotation_raises_at_decoration(isolated_registry) -> None:
    # The resolver's loud-failure invariant: an annotation that names something
    # undefined in the module (e.g. a TYPE_CHECKING-only import) must raise at
    # @imp_tool decoration time — never fall back to a degenerate schema.
    with pytest.raises(NameError):
        @imp_tool(name="ghost", description="x")
        def ghost(ctx: object, x: NoSuchType) -> str:  # noqa: F821
            return ""
