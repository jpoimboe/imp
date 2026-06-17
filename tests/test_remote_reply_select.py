from __future__ import annotations

from imp.cli.remote_channel import compose_on_event


def test_compose_on_event_fans_out_to_both() -> None:
    a: list[object] = []
    b: list[object] = []
    cb = compose_on_event(a.append, b.append)
    cb({"type": "x"})
    assert a == [{"type": "x"}] and b == [{"type": "x"}]
