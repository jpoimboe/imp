from __future__ import annotations

from imp.remote.daemon.subscriptions import Subscriptions


def test_add_makes_change_a_member() -> None:
    subs = Subscriptions()
    subs.add("aaa")
    assert subs.contains("aaa") is True
    assert subs.contains("bbb") is False


def test_all_returns_every_subscribed_id() -> None:
    subs = Subscriptions()
    subs.add("aaa")
    subs.add("bbb")
    assert set(subs.all()) == {"aaa", "bbb"}


def test_seen_reflects_the_replayed_id_set() -> None:
    subs = Subscriptions()
    subs.add("aaa", ["e1", "e2"])
    assert subs.seen("aaa", "e1") is True
    assert subs.seen("aaa", "e3") is False
    assert subs.seen("bbb", "e1") is False   # not subscribed → never seen


def test_remove_drops_membership_and_replayed_ids() -> None:
    subs = Subscriptions()
    subs.add("aaa", ["e1"])
    subs.remove("aaa")
    assert subs.contains("aaa") is False
    assert subs.seen("aaa", "e1") is False


def test_remove_unknown_change_is_a_noop() -> None:
    subs = Subscriptions()
    subs.remove("ghost")          # must not raise
    assert subs.contains("ghost") is False


def test_snapshot_and_add_registers_membership_and_ids_from_events() -> None:
    subs = Subscriptions()
    events = [{"id": "e1", "type": "agent_output"}, {"id": "e2", "type": "agent_output"}]
    returned = subs.snapshot_and_add("aaa", lambda: events)
    assert returned == events           # returns the snapshot for the caller to format
    assert subs.contains("aaa") is True
    assert subs.seen("aaa", "e1") is True
    assert subs.seen("aaa", "e2") is True
    assert subs.seen("aaa", "e3") is False


def test_mark_seen_records_an_id_for_a_subscribed_change() -> None:
    subs = Subscriptions()
    subs.add("aaa")
    assert subs.seen("aaa", "e1") is False   # not seen before marking
    subs.mark_seen("aaa", "e1")
    assert subs.seen("aaa", "e1") is True


def test_mark_seen_on_an_unsubscribed_change_grants_no_membership() -> None:
    subs = Subscriptions()
    subs.mark_seen("ghost", "e1")            # never subscribed
    assert subs.contains("ghost") is False   # must not create membership
    assert subs.seen("ghost", "e1") is False
