from __future__ import annotations

from imp.providers.codex_rollout import _canonical_tool_name


def test_canonical_strips_mcp_imp_prefix() -> None:
    assert _canonical_tool_name("mcp__imp__read_file") == "read_file"


def test_canonical_strips_single_server_prefix() -> None:
    assert _canonical_tool_name("imp__glob") == "glob"


def test_canonical_passes_bare_name_through() -> None:
    assert _canonical_tool_name("bash") == "bash"


def test_canonical_none_is_none() -> None:
    assert _canonical_tool_name(None) is None
