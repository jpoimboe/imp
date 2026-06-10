from __future__ import annotations

from imp.cli.main import _should_respawn_reader


def test_respawn_after_esc_only() -> None:
    # ESC fired → reader exited → respawn needed.
    assert _should_respawn_reader(reader_exited=True) is True
    # Ctrl-C → reader still parked → no respawn (reset() un-parks it).
    assert _should_respawn_reader(reader_exited=False) is False
