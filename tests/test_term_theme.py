# tests/test_term_theme.py
from __future__ import annotations

from imp.cli import term_theme as tt


def test_non_truecolor_terminal_is_ansi(monkeypatch) -> None:
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert tt.detect_theme() == "ansi"


def test_truecolor_with_dark_colorfgbg(monkeypatch) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert tt.detect_theme() == "dark"


def test_truecolor_with_light_colorfgbg(monkeypatch) -> None:
    monkeypatch.setenv("COLORTERM", "24bit")
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert tt.detect_theme() == "light"


def test_colorfgbg_parsing() -> None:
    assert tt._background_from_colorfgbg("15;0") == "dark"
    assert tt._background_from_colorfgbg("0;15") == "light"
    assert tt._background_from_colorfgbg("15;default;0") == "dark"   # 3-field form
    assert tt._background_from_colorfgbg("12;8") == "dark"           # 8 is dark
    assert tt._background_from_colorfgbg("0;7") == "light"           # 7 is light
    assert tt._background_from_colorfgbg(None) is None
    assert tt._background_from_colorfgbg("nonsense") is None
    assert tt._background_from_colorfgbg("12;x") is None


def test_osc11_query_returns_none_without_tty(monkeypatch) -> None:
    # No TTY in the test harness → the query bails immediately, never hangs.
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("COLORFGBG", raising=False)
    # background detection yields None → default dark (the truecolor default).
    assert tt._query_osc11_background(timeout=0.05) is None
    assert tt.detect_theme(timeout=0.05) == "dark"


def test_luminance_dark_vs_light() -> None:
    assert tt._luminance((0x1e, 0x1e, 0x1e)) < 0.5      # near-black bg
    assert tt._luminance((0xff, 0xff, 0xff)) >= 0.5     # white bg
    assert tt._luminance((0xfa, 0xfa, 0xfa)) >= 0.5     # off-white bg


def test_parse_osc11_components() -> None:
    # 4-digit-per-channel form (xterm's usual reply width).
    assert tt._parse_components(("1e1e", "1e1e", "1e1e")) == (0x1e, 0x1e, 0x1e)
    assert tt._parse_components(("ffff", "ffff", "ffff")) == (255, 255, 255)
    # 2-digit form also scales correctly.
    assert tt._parse_components(("ff", "ff", "ff")) == (255, 255, 255)
    assert tt._parse_components(("zz", "00", "00")) is None
