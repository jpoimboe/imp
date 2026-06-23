from __future__ import annotations

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
