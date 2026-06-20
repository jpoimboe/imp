# tests/test_palette.py
from __future__ import annotations

import io

import pytest

from imp.cli import palette as pal


@pytest.fixture(autouse=True)
def _reset_theme():
    """Every test starts from the default `ansi` theme; restore it after, so a
    test that flips to dark/light can't leak truecolor codes into the next."""
    pal.set_theme("ansi")
    yield
    pal.set_theme("ansi")


def test_default_theme_is_ansi_named_codes() -> None:
    assert pal.active_theme() == "ansi"
    assert pal._ANSI["magenta"] == "\x1b[35m"
    assert pal._ANSI["gray"] == "\x1b[90m"


def test_dark_theme_emits_truecolor() -> None:
    pal.set_theme("dark")
    assert pal.active_theme() == "dark"
    # magenta becomes the custom muted-mauve truecolor sequence (#a884b5), not the
    # named code.
    assert pal._ANSI["magenta"] == "\x1b[38;2;168;132;181m"
    assert pal.ansi("hi", "magenta", enabled=True) == "\x1b[38;2;168;132;181mhi\x1b[0m"
    # attributes are theme-independent.
    assert pal._ANSI["bold"] == "\x1b[1m"


def test_light_theme_differs_from_dark() -> None:
    pal.set_theme("dark")
    dark = pal._ANSI["green"]
    pal.set_theme("light")
    light = pal._ANSI["green"]
    assert dark != light
    assert light.startswith("\x1b[38;2;")    # still truecolor, different RGB


def test_unknown_theme_falls_back_to_ansi() -> None:
    pal.set_theme("rainbow")
    assert pal.active_theme() == "ansi"
    assert pal._ANSI["cyan"] == "\x1b[36m"


def test_ansi_disabled_returns_plain() -> None:
    assert pal.ansi("hi", "red", enabled=False) == "hi"


def test_ansi_enabled_wraps_and_resets_once() -> None:
    assert pal.ansi("hi", "bold", "magenta", enabled=True) == "\x1b[1m\x1b[35mhi\x1b[0m"


class _TTY:
    def isatty(self) -> bool:
        return True


def test_should_use_color_requires_tty_and_no_NO_COLOR(monkeypatch) -> None:
    for var in ("NO_COLOR", "CLICOLOR_FORCE", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    assert pal.should_use_color(io.StringIO()) is False     # not a tty
    assert pal.should_use_color(_TTY()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert pal.should_use_color(_TTY()) is False


@pytest.mark.parametrize("force_var", ["CLICOLOR_FORCE", "FORCE_COLOR"])
def test_should_use_color_force_var_overrides_pipe(monkeypatch, force_var) -> None:
    # Force vars color even a non-tty (e.g. `imp log | less -R`)...
    for var in ("NO_COLOR", "CLICOLOR_FORCE", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(force_var, "1")
    assert pal.should_use_color(io.StringIO()) is True
    # ...but NO_COLOR still wins.
    monkeypatch.setenv("NO_COLOR", "1")
    assert pal.should_use_color(io.StringIO()) is False


def test_action_display_text() -> None:
    assert pal.action_text({"verb": "continue"}) == "continue"
    assert pal.action_text({"verb": "goto", "target": "impl"}) == "goto(impl)"
    assert pal.action_text({"verb": "pause"}) == "pause"
    assert pal.action_text({"verb": "goto"}) == "goto"   # no target → bare verb
    assert pal.action_text("continue") == "continue"     # bare string fallback


def test_action_text_goto_resume_marker() -> None:
    assert pal.action_text(
        {"verb": "goto", "target": "coder", "resume": True}) == "goto(coder) [resume]"
    # resume false (or absent) renders the plain goto
    assert pal.action_text(
        {"verb": "goto", "target": "coder", "resume": False}) == "goto(coder)"
    assert pal.action_text({"verb": "goto", "target": "coder"}) == "goto(coder)"


def test_fold_glyphs_default_unicode() -> None:
    assert pal.fold_glyph(expanded=False) == "▸"
    assert pal.fold_glyph(expanded=True) == "▾"


def test_node_colors_calm_scheme() -> None:
    # The most frequent content is calmest; weight/color go to identity + structure.
    assert pal.NODE_COLORS["agent"] == ()                  # plain default fg (was bold)
    assert pal.NODE_COLORS["user"] == ("bright_blue",)     # bright-blue caret marks user turn
    assert pal.NODE_COLORS["phase"] == ("bold", "blue")    # structure stands out: bold blue
    assert pal.NODE_COLORS["step"] == ("bold", "step")     # bold darker-blue "mini-phase" label
    assert pal.NODE_COLORS["subagent"] == ("magenta",)     # unchanged
    assert pal.NODE_COLORS["workflow"] == ("bold", "magenta")  # lone bold anchor, unchanged
    # Semantic colors untouched.
    assert pal.NODE_COLORS["error"] == ("bold", "red")
    assert pal.NODE_COLORS["warning"] == ("yellow",)


def test_agent_color_is_empty_so_ansi_is_passthrough() -> None:
    # agent = () means pal.ansi returns prose unwrapped even when color is on.
    assert pal.ansi("prose", *pal.NODE_COLORS["agent"], enabled=True) == "prose"


def test_user_color_is_bright_blue_foreground() -> None:
    # User-turn identity is now a bright-blue FOREGROUND caret (not a bg fill):
    # the live prompt (slash/prompt) and the imp-log replay both reference this.
    assert pal.NODE_COLORS["user"] == ("bright_blue",)
    assert pal._ANSI["bright_blue"] == "\x1b[94m"


def test_caret_and_rule_helpers() -> None:
    # The input-field chrome: the caret is empty (the ┃ bar is the sole user-turn
    # marker), so caret() is "" with no stray escapes regardless of color. The
    # gray rule's dashes are structural, kept regardless.
    assert pal.caret(enabled=True) == ""
    assert pal.caret(enabled=False) == ""
    assert pal.rule(4, enabled=True) == "\x1b[90m────\x1b[0m"
    assert pal.rule(4, enabled=False) == "────"


def test_clip_display_returns_fitting_frame_byte_identical() -> None:
    s = "\x1b[36m⠋\x1b[0m hello"
    assert pal.clip_display(s, 80) == s            # fits → unchanged, no extra reset


def test_clip_display_truncates_on_display_columns_with_ellipsis() -> None:
    out = pal.clip_display("abcdefghij", 5)
    assert out == "abcd…"                          # 5 display columns total


def test_clip_display_color_off_adds_no_reset() -> None:
    out = pal.clip_display("abcdefghij", 5)        # no SGR present
    assert "\x1b[0m" not in out


def test_clip_display_with_sgr_appends_single_reset_when_clipped() -> None:
    out = pal.clip_display("\x1b[90mabcdefghij\x1b[0m", 5)
    assert out.endswith("…\x1b[0m")
    assert out.count("\x1b[0m") == 1


def test_clip_display_counts_wide_chars_as_two_columns() -> None:
    # two CJK chars = 4 display columns; width 3 keeps one wide char + ellipsis
    out = pal.clip_display("漢字", 3)
    assert out == "漢…"


def _lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def test_dark_hierarchy_phase_above_agent_header_above_tool_chrome() -> None:
    # On a dark surface, more prominent = brighter. Hierarchy:
    # phase (blue) > agent-model header (step) > tool-summary (gray).
    d = pal._DARK_RGB
    assert _lum(d["blue"]) > _lum(d["step"]) > _lum(d["gray"])
    # change header (magenta, bold) must outrank phase (blue) in luminance too
    assert _lum(d["magenta"]) > _lum(d["blue"])


def test_dark_phase_and_step_contrast_is_raised() -> None:
    # The overhaul RAISES phase/agent-header contrast above the muted pre-overhaul
    # blues: each now clears the gray chrome by a wide margin (old gaps were
    # blue-gray ~= 52 and step-gray ~= 21).
    d = pal._DARK_RGB
    assert _lum(d["blue"]) - _lum(d["gray"]) >= 55
    assert _lum(d["step"]) - _lum(d["gray"]) >= 40


def test_light_hierarchy_phase_above_agent_header_above_tool_chrome() -> None:
    # On a light surface, more prominent = DARKER (lower luminance). The same
    # hierarchy therefore reads as ascending luminance. (Pre-overhaul this was
    # inverted: step was darker than blue.)
    light = pal._LIGHT_RGB
    assert _lum(light["blue"]) < _lum(light["step"]) < _lum(light["gray"])


def test_user_bar_is_blue_glyph_under_color_and_space_off() -> None:
    assert pal.user_bar(enabled=True) == "\x1b[94m┃\x1b[0m"
    assert pal.user_bar(enabled=False) == " "   # blank gutter when color is off


def test_lifecycle_role_is_nonbold_yellow_in_all_themes() -> None:
    # named role over yellow, non-bold — must resolve identically in all themes.
    assert pal.NODE_COLORS["lifecycle"] == ("yellow",)
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        assert pal.ansi("x", *pal.NODE_COLORS["lifecycle"], enabled=True) == \
            pal.ansi("x", "yellow", enabled=True)
        assert pal._ANSI["bold"] not in pal.ansi("x", *pal.NODE_COLORS["lifecycle"],
                                                 enabled=True)


def test_spinner_frame_braille_default_and_ascii_and_wrap() -> None:
    braille = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert pal.spinner_frame(0) == "⠋"
    assert pal.spinner_frame(3) == "⠸"
    assert pal.spinner_frame(len(braille)) == "⠋"        # wraps at frame % len
    assert pal.spinner_frame(0, unicode=False) == "|"
    assert pal.spinner_frame(1, unicode=False) == "/"
    assert pal.spinner_frame(4, unicode=False) == "|"    # 4 % 4 == 0
