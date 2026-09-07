from __future__ import annotations

import pytest

from impty.screen import Regime, TerminalScreen
from impty.stream import TerminalStream

# Real sequences modern programs emit that pyte mis-parses. Each must vanish
# whole: no text drawn, no reply sent, no attribute left behind.
DROPPED = [
    b"\x1b[>c",       # secondary DA — tmux's startup burst
    b"\x1b[>0q",      # XTVERSION
    b"\x1b[>4;2m",    # XTMODKEYS — the source of the underline corruption
    b"\x1b[>1u",      # kitty keyboard protocol: push flags
    b"\x1b[<u",       # kitty keyboard protocol: pop flags
    b"\x1b[=5u",      # kitty keyboard protocol: set flags
    b"\x1bP0$r1;2m\x1b\\",             # DCS: a DECRQSS reply
    b"\x1bP+q544e\x1b\\",              # DCS: an XTGETTCAP reply
    b"\x1b_Gf=100,a=T;SGVsbG8=\x1b\\", # APC: kitty graphics
    b"\x1b^private\x1b\\",             # PM
    b"\x1bXsos\x1b\\",                 # SOS
]

# Every internal split of b"a" + seq + b"b", as its own case, so a boundary
# failure names the offending index instead of hiding inside a loop.
SPLITS = [(seq, split) for seq in DROPPED for split in range(1, len(seq) + 2)]


def _stream(cols: int = 20, rows: int = 6):
    screen = TerminalScreen(cols, rows, history=100, ratio=0.5)
    # A reply callback is required for the "no reply" assertion to mean
    # anything: with reply unset, write_process_input silently no-ops.
    replies: list[str] = []
    screen.reply = replies.append
    return screen, TerminalStream(screen), replies


def _assert_dropped(screen: TerminalScreen, replies: list[str]) -> None:
    assert screen.display[0].rstrip() == "ab"
    assert replies == []
    cell = screen.buffer[0][1]
    assert cell.data == "b"
    assert not cell.underscore
    assert not cell.bold
    assert not cell.reverse
    assert cell.fg == "default"
    assert cell.bg == "default"


@pytest.mark.parametrize("seq", DROPPED, ids=repr)
def test_sequence_is_dropped_whole(seq: bytes) -> None:
    screen, stream, replies = _stream()
    stream.feed(b"a" + seq + b"b")
    _assert_dropped(screen, replies)


@pytest.mark.parametrize("seq", DROPPED, ids=repr)
def test_sequence_is_dropped_one_byte_at_a_time(seq: bytes) -> None:
    screen, stream, replies = _stream()
    payload = b"a" + seq + b"b"
    for i in range(len(payload)):
        stream.feed(payload[i : i + 1])
    _assert_dropped(screen, replies)


@pytest.mark.parametrize(("seq", "split"), SPLITS, ids=repr)
def test_sequence_is_dropped_across_a_chunk_boundary(seq: bytes, split: int) -> None:
    screen, stream, replies = _stream()
    payload = b"a" + seq + b"b"
    stream.feed(payload[:split])
    stream.feed(payload[split:])
    _assert_dropped(screen, replies)


def test_osc_title_still_passes_through() -> None:
    screen, stream, _ = _stream()
    stream.feed(b"\x1b]0;hello\x07")
    assert screen.title == "hello"


def test_question_prefixed_csi_still_reaches_pyte() -> None:
    screen, stream, _ = _stream()
    stream.feed(b"\x1b[?1049h")
    assert screen.regime is Regime.SCREEN


def test_plain_sgr_still_applies() -> None:
    screen, stream, _ = _stream()
    stream.feed(b"\x1b[1mX")
    assert screen.buffer[0][0].bold is True


def test_malformed_private_csi_hands_an_embedded_c0_to_pyte() -> None:
    # \r\n arriving mid-sequence must execute — a real terminal executes C0
    # controls inside a CSI — not vanish with the aborted drop.
    screen, stream, _ = _stream()
    stream.feed(b"\x1b[>1\r\nX")
    assert screen.cursor.y == 1
    assert screen.display[1].rstrip() == "X"


def test_malformed_private_csi_hands_an_embedded_esc_to_pyte() -> None:
    # The ESC ends the drop and opens a fresh sequence, so the SGR still bolds.
    screen, stream, _ = _stream()
    stream.feed(b"\x1b[>1\x1b[1mZ")
    assert screen.display[0].rstrip() == "Z"
    assert screen.buffer[0][0].bold is True


def test_string_sequence_terminated_by_bel_is_dropped() -> None:
    screen, stream, replies = _stream()
    stream.feed(b"a\x1bP+q544e\x07b")
    _assert_dropped(screen, replies)


def test_esc_inside_a_string_payload_opens_a_fresh_sequence() -> None:
    # xterm's abort rule: the ESC ends the string, and not being followed by
    # '\' it starts a new sequence — so the SGR must still apply to Z.
    screen, stream, _ = _stream()
    stream.feed(b"a\x1bPpayload\x1b[1mZ")
    assert screen.display[0].rstrip() == "aZ"
    assert screen.buffer[0][1].bold is True
