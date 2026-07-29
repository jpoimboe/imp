from __future__ import annotations

import subprocess
import sys

from imp.agents.tools import get_registry

_BUILTIN_TOOLS = [
    "bash", "bash_kill", "bash_output", "create_and_enter_worktree",
    "edit_file", "glob", "grep", "read_file", "rename_quest",
    "save_and_exit", "symbol_outline", "update_step_label", "write_file",
]


def test_all_builtins_registered() -> None:
    reg = get_registry()
    expected = {
        "read_file", "write_file", "edit_file",
        "glob", "grep",
        "bash",
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
                 "glob", "grep", "rename_quest"):
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
    # get_registry() below would otherwise latch the flag while the fake is
    # installed, leaving symbol_outline unregistered for the whole process.
    monkeypatch.setattr(tools, "_builtins_loaded", False)
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


def test_removed_introspection_tools_are_absent() -> None:
    reg = get_registry()
    assert "imp_events" not in reg
    assert "imp_show" not in reg


def test_grep_schema_exports_output_mode_enum() -> None:
    # The exported JSON schema is what steers the model — the Literal enum
    # must survive into it (the Task-1 resolver makes this possible).
    schema = get_registry()["grep"].input_schema
    assert schema["properties"]["output_mode"]["enum"] == [
        "files_with_matches", "content", "count"]


def test_importing_the_package_registers_nothing() -> None:
    """Importing imp.agents.tools must load no builtin tool module; the first
    get_registry() loads them all. Fresh interpreter: in-process, an earlier
    test in this worker has already imported them."""
    code = (
        "import sys, imp.agents.tools as tools\n"
        "eager = sorted(n for n in sys.modules if n.startswith('imp.agents.tools.'))\n"
        "print('EAGER:' + ','.join(eager))\n"
        "print('AFTER:' + ','.join(sorted(tools.get_registry())))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    reported = {
        key: value
        for key, _, value in (ln.partition(":") for ln in proc.stdout.splitlines())
        if key in {"EAGER", "AFTER"}
    }
    assert reported.keys() == {"EAGER", "AFTER"}, (
        f"child did not report both lines; stdout was {proc.stdout!r}"
    )
    assert reported["EAGER"] == "", f"import imp.agents.tools eagerly loaded: {reported['EAGER']}"
    assert reported["AFTER"] == ",".join(_BUILTIN_TOOLS)


def test_partial_registration_still_loads_every_builtin() -> None:
    """Importing one tool module registers only its own tools; get_registry()
    must still load the full set. This is why the guard is a flag and not an
    emptiness check on _REGISTRY, which @imp_tool fills incrementally."""
    code = (
        "import imp.agents.tools.fs\n"
        "import imp.agents.tools as tools\n"
        "print('PARTIAL:' + ','.join(sorted(tools._REGISTRY)))\n"
        "print('FULL:' + ','.join(sorted(tools.get_registry())))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    reported = {
        key: value
        for key, _, value in (ln.partition(":") for ln in proc.stdout.splitlines())
        if key in {"PARTIAL", "FULL"}
    }
    assert reported.keys() == {"PARTIAL", "FULL"}, (
        f"child did not report both lines; stdout was {proc.stdout!r}"
    )
    assert reported["PARTIAL"] == "edit_file,read_file,write_file"
    assert reported["FULL"] == ",".join(_BUILTIN_TOOLS)
