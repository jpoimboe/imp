from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ViewportState:
    follow: bool = True
    scroll: int = 0          # desired top line; consulted only when not following

    def vertical_scroll(self, *, total_lines: int, height: int) -> int:
        """Top line to render: tail when following, else clamped stored offset."""
        bottom = max(0, total_lines - height)
        if self.follow:
            return bottom
        return min(max(0, self.scroll), bottom)

    def page_up(self, *, total_lines: int, height: int) -> None:
        base = self.vertical_scroll(total_lines=total_lines, height=height)
        self.follow = False
        self.scroll = max(0, base - height)

    def page_down(self, *, total_lines: int, height: int) -> None:
        bottom = max(0, total_lines - height)
        base = self.vertical_scroll(total_lines=total_lines, height=height)
        self.scroll = min(base + height, bottom)

    def scroll_up_lines(self, n: int, *, total_lines: int, height: int) -> None:
        base = self.vertical_scroll(total_lines=total_lines, height=height)
        self.follow = False
        self.scroll = max(0, base - n)

    def scroll_down_lines(self, n: int, *, total_lines: int, height: int) -> None:
        bottom = max(0, total_lines - height)
        base = self.vertical_scroll(total_lines=total_lines, height=height)
        self.scroll = min(base + n, bottom)

    def jump_to_bottom(self) -> None:
        self.follow = True
        self.scroll = 0
