from __future__ import annotations

import threading

from imp.remote.daemon.focus import Focus


def test_current_is_none_initially() -> None:
    assert Focus().current() is None


def test_set_then_current_returns_the_id() -> None:
    f = Focus()
    f.focus_on("2026-06-14-aaaaaa")
    assert f.current() == "2026-06-14-aaaaaa"


def test_set_replaces_any_prior_focus() -> None:
    f = Focus()
    f.focus_on("2026-06-14-aaaaaa")
    f.focus_on("2026-06-14-bbbbbb")
    assert f.current() == "2026-06-14-bbbbbb"


def test_clear_is_unconditional() -> None:
    f = Focus()
    f.focus_on("2026-06-14-aaaaaa")
    f.clear()
    assert f.current() is None


def test_clear_if_clears_only_the_matching_id() -> None:
    f = Focus()
    f.focus_on("2026-06-14-aaaaaa")
    f.clear_if("2026-06-14-bbbbbb")
    assert f.current() == "2026-06-14-aaaaaa"
    f.clear_if("2026-06-14-aaaaaa")
    assert f.current() is None


def test_clear_if_on_empty_focus_is_a_noop() -> None:
    f = Focus()
    f.clear_if("2026-06-14-aaaaaa")
    assert f.current() is None


def test_concurrent_set_and_clear_if_is_thread_safe() -> None:
    f = Focus()
    ids = [f"2026-06-14-{i:06d}" for i in range(40)]

    def worker(cid: str) -> None:
        for _ in range(2000):
            f.focus_on(cid)
            f.clear_if(cid)

    threads = [threading.Thread(target=worker, args=(c,)) for c in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert f.current() in set(ids) | {None}
