from __future__ import annotations

from imp.agents.tools import get_registry


def test_all_builtins_registered() -> None:
    reg = get_registry()
    expected = {
        "read_file", "write_file", "edit_file",
        "glob", "grep",
        "bash",
        "query_events",
        "symbol_outline",
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
                 "glob", "grep", "query_events", "rename_change"):
        schema = reg[name].input_schema
        assert schema.get("type") == "object", name
        assert "properties" in schema, name


def test_register_builtins_isolates_a_broken_module(monkeypatch, caplog) -> None:
    # A built-in tool module that fails to import is logged and skipped, never
    # fatal — no single tool can crash registration for all the others.
    import importlib

    import imp.agents.tools as tools

    real_import = importlib.import_module

    def _fake(name, *args, **kwargs):
        if name == "imp.agents.tools.symbol_outline":
            raise RuntimeError("boom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _fake)
    with caplog.at_level("WARNING"):
        tools._register_builtins()  # must not raise despite the broken module

    assert any(
        "symbol_outline" in r.getMessage() for r in caplog.records
    ), "the skipped module should be logged"
    # Siblings registered at the real module load are still available.
    assert "bash" in tools.get_registry()


def test_symbol_outline_in_base_caps() -> None:
    from imp.engine.loader import BASE_CAPS
    assert "symbol_outline" in BASE_CAPS


def test_grep_schema_exports_output_mode_enum() -> None:
    # The exported JSON schema is what steers the model — the Literal enum
    # must survive into it (the Task-1 resolver makes this possible).
    schema = get_registry()["grep"].input_schema
    assert schema["properties"]["output_mode"]["enum"] == [
        "files_with_matches", "content", "count"]
