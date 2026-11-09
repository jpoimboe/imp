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


# CSI sequences carrying an intermediate byte (0x20-0x2F). pyte treats the
# intermediate as the final, so the real final leaks: drawn, or run as another
# command. Each must vanish whole; the text after it renders normally.
INTERMEDIATE = [
    (b"x\x1b[!py", "xy"),            # DECSTR, sent by reset(1)
    (b"x\x1b[1\"py", "xy"),          # DECSCL
    (b"abc\x1b[1G\x1b[2 @", "abc"),  # SL; pyte runs it as ICH
    (b"x\x1b[2 qy", "xy"),           # DECSCUSR; pyte happens to skip SP
    (b"x\x1b[?2026$py", "xy"),       # DECRQM, `?`-prefixed
]


@pytest.mark.parametrize(("payload", "expected"), INTERMEDIATE, ids=repr)
def test_intermediate_byte_csi_is_dropped_whole(payload: bytes, expected: str) -> None:
    screen, stream, replies = _stream()
    stream.feed(payload)
    assert screen.display[0].rstrip() == expected
    assert replies == []


@pytest.mark.parametrize(("payload", "expected"), INTERMEDIATE, ids=repr)
def test_intermediate_byte_csi_is_dropped_one_byte_at_a_time(
    payload: bytes, expected: str
) -> None:
    screen, stream, replies = _stream()
    for i in range(len(payload)):
        stream.feed(payload[i : i + 1])
    assert screen.display[0].rstrip() == expected
    assert replies == []


def test_parameterised_csi_split_across_chunks_still_applies() -> None:
    # Held parameter bytes are released whole once the final byte arrives.
    screen, stream, _ = _stream()
    stream.feed(b"ab\x1b[4")
    stream.feed(b";1")
    stream.feed(b"2Hz")
    assert screen.display[3].rstrip() == " " * 11 + "z"


def test_malformed_csi_hands_held_bytes_and_an_embedded_c0_to_pyte() -> None:
    # A C0 aborts the hold: pyte gets exactly the bytes master gave it, and
    # executes the CR/LF inside the CSI as a real terminal does.
    screen, stream, _ = _stream()
    stream.feed(b"\x1b[2\r\nC")
    assert (screen.cursor.x, screen.cursor.y) == (2, 1)


# Aborted or non-leading-private CSI: pyte gets the same bytes it got before CSI
# parameters were held. (payload, row 0, cursor x, replies)
HELD_CSI = [
    (b"\x1b[1\x1b[2mX", "[2mX", 4, []),  # ESC restarts: the first CSI is cut off
    (b"\x1b[1\x18X", "X", 1, []),  # CAN
    (b"\x1b[1\x1aX", "X", 1, []),  # SUB
    (b"\x1b[0>cX", "X", 1, ["\x1b[?6c"]),  # '>' after a parameter is ordinary
    (b"\x1b[6>nX", "X", 1, ["\x1b[1;1R"]),
    (b"\x1b[1<cX", "cX", 2, []),
    (b"\x1b[1=cX", "cX", 2, []),
    (b"ab\x1b[1?KX", "  X", 3, []),  # '?' after a parameter: still erases to cursor
]
HELD_CSI_IDS = [repr(payload) for payload, *_ in HELD_CSI]


@pytest.mark.parametrize(("payload", "row", "x", "expected"), HELD_CSI, ids=HELD_CSI_IDS)
def test_held_csi_abort_matches_what_pyte_always_saw(
    payload: bytes, row: str, x: int, expected: list[str]
) -> None:
    screen, stream, replies = _stream()
    stream.feed(payload)
    assert (screen.display[0].rstrip(), screen.cursor.x, replies) == (row, x, expected)


@pytest.mark.parametrize(("payload", "row", "x", "expected"), HELD_CSI, ids=HELD_CSI_IDS)
def test_held_csi_abort_one_byte_at_a_time_matches_whole_feed(
    payload: bytes, row: str, x: int, expected: list[str]
) -> None:
    screen, stream, replies = _stream()
    for i in range(len(payload)):
        stream.feed(payload[i : i + 1])
    assert (screen.display[0].rstrip(), screen.cursor.x, replies) == (row, x, expected)
