from __future__ import annotations

from imp.remote.daemon.alert_all import AlertAll


def test_default_is_off() -> None:
    assert AlertAll().enabled() is False


def test_set_true_enables() -> None:
    a = AlertAll()
    a.set(True)
    assert a.enabled() is True


def test_set_false_disables() -> None:
    a = AlertAll()
    a.set(True)
    a.set(False)
    assert a.enabled() is False


def test_seen_is_false_until_marked() -> None:
    assert AlertAll().seen("c1", "e1") is False


def test_mark_then_seen_is_true() -> None:
    a = AlertAll()
    a.mark("c1", "e1")
    assert a.seen("c1", "e1") is True


def test_mark_is_keyed_on_both_change_and_event() -> None:
    a = AlertAll()
    a.mark("c1", "e1")
    assert a.seen("c1", "e2") is False
    assert a.seen("c2", "e1") is False


def test_set_false_clears_the_delivery_log() -> None:
    a = AlertAll()
    a.mark("c1", "e1")
    a.set(False)
    assert a.seen("c1", "e1") is False
