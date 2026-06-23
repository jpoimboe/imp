from __future__ import annotations

from collections.abc import Sequence

from imp.cli import opening


def test_wordmark_half_block_is_the_exact_three_rows() -> None:
    out = opening.render_wordmark(theme="ansi", color=False, unicode=True)
    assert out == "▀\n█ █▀█▀█ █▀█\n▀ ▀ ▀ ▀ █▀▀"


def test_wordmark_half_block_colored_wraps_each_row_in_wordmark_dark() -> None:
    gold = "\x1b[38;2;255;154;60m"
    out = opening.render_wordmark(theme="dark", color=True, unicode=True)
    assert out == (f"{gold}▀\x1b[0m\n"
                   f"{gold}█ █▀█▀█ █▀█\x1b[0m\n"
                   f"{gold}▀ ▀ ▀ ▀ █▀▀\x1b[0m")


def test_wordmark_ascii_fallback_is_plain_word_imp() -> None:
    assert opening.render_wordmark(theme="ansi", color=False, unicode=False) == "imp"


def test_wordmark_ascii_fallback_colored_is_bold_wordmark() -> None:
    out = opening.render_wordmark(theme="ansi", color=True, unicode=False)
    assert out == "\x1b[1m\x1b[33mimp\x1b[0m"


class _IndexRng:
    """A stub rng whose choice() always returns a fixed index of the sequence."""

    def __init__(self, index: int) -> None:
        self._index = index

    def choice(self, seq: Sequence[str]) -> str:
        return seq[self._index]


def test_phrase_pool_has_twenty_unique_phrases() -> None:
    assert len(opening.PHRASES) == 20
    assert len(set(opening.PHRASES)) == 20


def test_pick_phrase_selects_from_the_pool_via_injected_rng() -> None:
    assert opening.pick_phrase(_IndexRng(0)) == "a familiar, summoned to help you ship."
    assert opening.pick_phrase(_IndexRng(15)) == "what are we building tonight?"
    assert opening.pick_phrase(_IndexRng(19)) == "your imp is listening."


def test_pick_phrase_default_rng_returns_a_pool_member() -> None:
    assert opening.pick_phrase() in opening.PHRASES
