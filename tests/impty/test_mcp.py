from __future__ import annotations

import asyncio
import os
import sys

import pytest

import impty.mcp as mcp_mod
from impty.mcp import _decode_keys


def _can_pty() -> bool:
    try:
        import pty

        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


def test_decodes_carriage_return_so_enter_submits() -> None:
    # The send() contract documents Enter=\r; the agent→MCP JSON boundary
    # delivers a written "\r" as the two literal chars backslash + 'r', so the
    # tool must decode it into a real carriage return.
    assert _decode_keys("foo\\r") == "foo\r"


def test_decodes_hex_and_escape_mnemonics() -> None:
    assert _decode_keys("\\x03") == "\x03"  # Ctrl-C
    assert _decode_keys("\\t") == "\t"  # Tab
    assert _decode_keys("\\x1b[B") == "\x1b[B"  # Down arrow: ESC then literal [B


def test_leaves_plain_text_and_unicode_untouched() -> None:
    # A keystroke tool mustn't corrupt ordinary typed text, including non-ASCII.
    assert _decode_keys("café résumé") == "café résumé"


def test_leaves_unrecognized_backslash_literal() -> None:
    # A lone backslash that isn't a known escape stays as-is (no crash).
    assert _decode_keys("a\\z b") == "a\\z b"


def test_transcript_tool_is_removed_view_remains() -> None:
    names = {t.name for t in asyncio.run(mcp_mod.mcp.list_tools())}
    assert "transcript" not in names
    assert {"start", "view", "send", "wait", "snapshot", "stop"} <= names


@pytest.mark.skipif(not _can_pty(), reason="pty not available")
def test_view_returns_current_screen_by_default_and_full_on_demand() -> None:
    prog = "import time\n[print('line', i) for i in range(40)]\ntime.sleep(1)"
    mcp_mod.start([sys.executable, "-c", prog])
    try:
        v = mcp_mod.view()
        assert "regime=scroll" in v.splitlines()[0]   # status header reports regime
        assert "line 39" in v                          # the live window
        assert "line 0" not in v                       # whole strip NOT dumped by default
        full = mcp_mod.view(full=True)
        assert "line 0" in full                        # full strip on demand
    finally:
        mcp_mod.stop()
