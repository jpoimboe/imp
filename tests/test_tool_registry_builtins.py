from __future__ import annotations

from imp.agents.tools import get_registry


def test_all_builtins_registered() -> None:
    reg = get_registry()
    expected = {
        "read_file", "write_file", "edit_file",
        "glob", "grep", "ls",
        "bash",
        "write_todos",
        "ask_user",
        "query_events", "web_fetch", "git_diff",
    }
    missing = expected - reg.keys()
    assert not missing, f"missing built-in tools: {sorted(missing)}"
