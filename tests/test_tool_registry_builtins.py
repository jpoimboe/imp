from __future__ import annotations

from imp.agents.tools import get_registry


def test_all_builtins_registered() -> None:
    reg = get_registry()
    expected = {
        "read_file", "write_file", "edit_file",
        "glob", "grep", "ls",
        "bash",
        "write_todos",
        "query_events",
    }
    missing = expected - reg.keys()
    assert not missing, f"missing built-in tools: {sorted(missing)}"
    assert "ask_user" not in reg, "ask_user should no longer be a built-in tool"


def test_full_builtin_set_schema_builds_under_resolver() -> None:
    # The resolver fix re-evaluates EVERY @imp_tool function's annotations at
    # import time (the _register_builtins set). A TYPE_CHECKING-only
    # annotation in any of these modules would now crash the import —
    # deliberate, loud invariant. This test pins that the whole set builds
    # non-degenerate object schemas.
    reg = get_registry()
    for name in ("bash", "read_file", "write_file", "edit_file",
                 "glob", "grep", "ls", "query_events", "rename_change",
                 "write_todos"):
        schema = reg[name].input_schema
        assert schema.get("type") == "object", name
        assert "properties" in schema, name
