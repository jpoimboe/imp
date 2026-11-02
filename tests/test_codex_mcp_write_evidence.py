"""Non-live unit test for the MCP-write evidence helper in tests/_live.py.

Not `live`-marked: the codex probe that consumes this helper cannot run in CI,
so the verifiable coverage lives here, driving the pure helper with synthetic
events.jsonl records and a tmp dir."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._live import mcp_write_miss


def _call(cid: str, name: str, args: dict[str, Any] | str) -> dict[str, Any]:
    return {"type": "tool_call",
            "payload": {"id": cid, "name": name, "input": args}}


def _result(cid: str, *, error: bool) -> dict[str, Any]:
    return {"type": "tool_result",
            "payload": {"id": cid, "content": "x", "error": error}}


_COMPLETE = [_call("c9", "complete_step", {"patch_error": "p", "exec_error": "e"}),
             _result("c9", error=False)]


def test_no_write_file_call_is_a_miss(tmp_path: Path) -> None:
    # No mcp_written.txt exists: a file read would raise FileNotFoundError.
    assert mcp_write_miss(tmp_path, _COMPLETE) == (
        "never called write_file on mcp_written.txt "
        "(tool calls: ['complete_step'])")


def test_write_to_other_path_is_a_miss(tmp_path: Path) -> None:
    (tmp_path / "other.txt").write_text("MCP-OK")
    records = [_call("c1", "write_file", {"path": "other.txt", "content": "MCP-OK"}),
               _result("c1", error=False), *_COMPLETE]
    assert mcp_write_miss(tmp_path, records) == (
        "never called write_file on mcp_written.txt "
        "(tool calls: ['write_file', 'complete_step'])")


def test_ok_result_but_file_absent_raises(tmp_path: Path) -> None:
    records = [_call("c1", "write_file",
                     {"path": "mcp_written.txt", "content": "MCP-OK"}),
               _result("c1", error=False), *_COMPLETE]
    with pytest.raises((AssertionError, FileNotFoundError)):
        mcp_write_miss(tmp_path, records)


def test_error_result_raises(tmp_path: Path) -> None:
    (tmp_path / "mcp_written.txt").write_text("MCP-OK")
    records = [_call("c1", "write_file",
                     {"path": "mcp_written.txt", "content": "MCP-OK"}),
               _result("c1", error=True), *_COMPLETE]
    with pytest.raises(AssertionError):
        mcp_write_miss(tmp_path, records)


def test_content_differing_from_call_argument_raises(tmp_path: Path) -> None:
    (tmp_path / "mcp_written.txt").write_text("STALE")
    records = [_call("c1", "write_file",
                     {"path": "mcp_written.txt", "content": "MCP-OK"}),
               _result("c1", error=False), *_COMPLETE]
    with pytest.raises(AssertionError):
        mcp_write_miss(tmp_path, records)


@pytest.mark.parametrize("shape", ["dict", "json_string", "absolute_path"])
def test_landed_write_is_not_a_miss(tmp_path: Path, shape: str) -> None:
    # The model's own content, not the literal MCP-OK, is the expectation.
    (tmp_path / "mcp_written.txt").write_text("hello")
    args: dict[str, Any] | str = {"path": "mcp_written.txt", "content": "hello"}
    if shape == "json_string":
        args = json.dumps(args)
    elif shape == "absolute_path":
        args = {"path": str(tmp_path / "mcp_written.txt"), "content": "hello"}
    records = [_call("c1", "write_file", args), _result("c1", error=False),
               *_COMPLETE]
    assert mcp_write_miss(tmp_path, records) is None


def test_last_targeting_call_decides(tmp_path: Path) -> None:
    (tmp_path / "mcp_written.txt").write_text("MCP-OK")
    records = [_call("c1", "write_file",
                     {"path": "mcp_written.txt", "content": "MCP-OK"}),
               _result("c1", error=True),
               _call("c2", "write_file",
                     {"path": "./mcp_written.txt", "content": "MCP-OK"}),
               _result("c2", error=False), *_COMPLETE]
    assert mcp_write_miss(tmp_path, records) is None


def test_reused_item_id_pairs_with_following_result(tmp_path: Path) -> None:
    # codex item ids restart per turn, so an earlier errored call can share the id.
    (tmp_path / "mcp_written.txt").write_text("MCP-OK")
    records = [_call("item_1", "read_file", {"path": "x"}),
               _result("item_1", error=True),
               _call("item_1", "write_file",
                     {"path": "mcp_written.txt", "content": "MCP-OK"}),
               _result("item_1", error=False), *_COMPLETE]
    assert mcp_write_miss(tmp_path, records) is None


def test_crlf_content_is_compared_byte_exact(tmp_path: Path) -> None:
    (tmp_path / "mcp_written.txt").write_bytes(b"a\r\nb")
    records = [_call("c1", "write_file",
                     {"path": "mcp_written.txt", "content": "a\r\nb"}),
               _result("c1", error=False), *_COMPLETE]
    assert mcp_write_miss(tmp_path, records) is None
