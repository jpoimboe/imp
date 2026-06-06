from __future__ import annotations

from impty.mcp import _decode_keys


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
