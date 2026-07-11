from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import impty.mcp as mcp_mod
from impty.driver import ImptyDriver
from impty.mcp import THIN_DESCRIPTIONS, build_server, impty_tool_specs


def test_server_lists_the_eight_tools_and_drops_transcript() -> None:
    names = {t.name for t in asyncio.run(mcp_mod.mcp.list_tools())}
    assert "transcript" not in names
    assert {"start", "view", "send", "wait", "resize",
            "snapshot", "stop", "send_signal"} <= names


def test_tool_descriptions_come_from_driver_method_docstrings() -> None:
    tools = {t.name: t for t in asyncio.run(mcp_mod.mcp.list_tools())}
    driver = ImptyDriver()
    by_method = {m.__name__: m for m in driver.tool_methods()}
    for name, tool in tools.items():
        assert tool.description == by_method[name].__doc__


def test_thin_description_mapping_covers_exactly_the_tool_methods() -> None:
    assert set(THIN_DESCRIPTIONS) == {
        m.__name__ for m in ImptyDriver().tool_methods()}


def test_impty_tool_specs_pair_each_method_with_its_schema() -> None:
    driver = ImptyDriver()
    specs = impty_tool_specs(driver)
    assert [s.name for s in specs] == [
        "start", "view", "send", "send_signal", "resize",
        "wait", "snapshot", "stop"]
    start_spec = next(s for s in specs if s.name == "start")
    # Schema is the FastMCP-introspected JSON Schema of the bound method.
    assert "argv" in start_spec.input_schema["properties"]
    assert start_spec.input_schema["required"] == ["argv"]
    # The method is the live bound method, callable to drive the session.
    assert start_spec.method.__name__ == "start"


def test_build_server_registers_thread_offloaded_tools_that_are_callable() -> None:
    driver = ImptyDriver()
    server = build_server(driver)
    # The registered tool is async (thread-offloaded) and dispatches through the
    # FastMCP call path; a no-session call surfaces the driver's RuntimeError as a
    # ToolError carrying its message — proof the registered tool really runs the
    # driver method, not a stub.
    with pytest.raises(ToolError, match="no session"):
        asyncio.run(server.call_tool("view", {}))


def test_impty_tool_specs_match_build_server_introspection() -> None:
    driver = ImptyDriver()
    # build_server's rich default (standalone surface) keeps the full docstrings.
    rich = {t.name: t for t in asyncio.run(build_server(driver).list_tools())}
    driver_docs = {m.__name__: m.__doc__ for m in driver.tool_methods()}
    specs = impty_tool_specs(driver)
    assert {s.name for s in specs} == set(rich)
    assert len(specs) == len(rich)
    for spec in specs:
        tool = rich[spec.name]
        # Names and schemas stay identical across surfaces...
        assert spec.name == tool.name
        assert spec.input_schema == tool.inputSchema
        # ...but the provider surface carries the thin one-liner while the
        # standalone server keeps the full docstring.
        assert spec.description == THIN_DESCRIPTIONS[spec.name]
        assert tool.description == driver_docs[spec.name]
        assert spec.description != driver_docs[spec.name]
