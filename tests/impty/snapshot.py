from __future__ import annotations

from impty import TerminalSession, render


def snapshot(
    session: TerminalSession,
    path: str,
    *,
    light: bool = False,
    full: bool = False,
    palette: dict[str, tuple[int, int, int]] | None = None,
) -> str:
    """Render the session's current screen to a PNG at `path`; return `path`."""
    bg = fg = None
    if light:
        bg, fg = render.light_surface()
    with session.paused():
        return render.render_screen(
            session.screen, path, bg=bg, fg=fg, palette=palette, full=full)
