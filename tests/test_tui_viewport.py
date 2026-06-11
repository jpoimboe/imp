from __future__ import annotations

from imp.cli.tui_viewport import ViewportState


def test_follow_pins_view_to_bottom() -> None:
    vs = ViewportState()                         # follow=True by default
    assert vs.follow is True
    assert vs.vertical_scroll(total_lines=100, height=20) == 80


def test_vertical_scroll_clamps_when_content_shorter_than_height() -> None:
    vs = ViewportState()
    assert vs.vertical_scroll(total_lines=5, height=20) == 0


def test_page_up_disengages_follow_and_moves_up_from_bottom() -> None:
    vs = ViewportState()
    vs.page_up(total_lines=100, height=20)       # bottom is 80 → 80-20
    assert vs.follow is False
    assert vs.vertical_scroll(total_lines=100, height=20) == 60


def test_page_up_does_not_go_above_top() -> None:
    vs = ViewportState(follow=False, scroll=10)
    vs.page_up(total_lines=100, height=20)
    assert vs.vertical_scroll(total_lines=100, height=20) == 0


def test_page_down_moves_toward_bottom() -> None:
    vs = ViewportState(follow=False, scroll=40)
    vs.page_down(total_lines=100, height=20)
    assert vs.vertical_scroll(total_lines=100, height=20) == 60


def test_page_down_clamps_at_bottom() -> None:
    vs = ViewportState(follow=False, scroll=75)
    vs.page_down(total_lines=100, height=20)
    assert vs.vertical_scroll(total_lines=100, height=20) == 80


def test_scroll_up_lines_disengages_follow() -> None:
    vs = ViewportState()                         # following
    vs.scroll_up_lines(3, total_lines=100, height=20)
    assert vs.follow is False
    assert vs.vertical_scroll(total_lines=100, height=20) == 77


def test_jump_to_bottom_reengages_follow() -> None:
    vs = ViewportState(follow=False, scroll=10)
    vs.jump_to_bottom()
    assert vs.follow is True
    assert vs.vertical_scroll(total_lines=100, height=20) == 80
