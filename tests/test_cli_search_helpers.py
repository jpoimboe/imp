from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from imp.cli.main import _selected_search
from imp.cli.store import (
    HomeDigest,
    Readiness,
    SearchHit,
    SearchZones,
    _merged_after,
    _search_active,
    _search_archived,
    _search_backlog,
    zone_of,
)
from imp.engine import archive
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


def _archive(
    tmp_path: Path, quest_id: str, title: str,
    status: str = "abandoned", merge_commit: str | None = None,
) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("")
    quest_json = tmp_path / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps({
        "id": quest_id, "status": status, "step": "spec",
        "merge_commit": merge_commit}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _init_git_commits(root: Path) -> tuple[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "a.txt").write_text("a")
    git("add", "a.txt")
    git("commit", "-qm", "a")
    sha_a = git("rev-parse", "HEAD")
    (root / "b.txt").write_text("b")
    git("add", "b.txt")
    git("commit", "-qm", "b")
    sha_b = git("rev-parse", "HEAD")
    return sha_a, sha_b


def _ns(**kw: object) -> argparse.Namespace:
    base = {"active": False, "backlog": False, "completed": False,
            "abandoned": False, "archived": False}
    base.update(kw)
    return argparse.Namespace(**base)


_ALL_STATUSES = frozenset({"completed", "abandoned", "archived"})


def test_selected_search_defaults_to_active_backlog_and_all_archive() -> None:
    assert _selected_search(_ns(), None) == SearchZones(True, True, _ALL_STATUSES, None)


def test_selected_search_single_zone_scopes() -> None:
    assert _selected_search(_ns(backlog=True), None) == SearchZones(
        False, True, frozenset(), None)
    assert _selected_search(_ns(active=True), None) == SearchZones(
        True, False, frozenset(), None)


def test_selected_search_disposition_flags_map_to_statuses() -> None:
    for status in ("completed", "abandoned", "archived"):
        assert _selected_search(_ns(**{status: True}), None) == SearchZones(
            False, False, frozenset({status}), None)


def test_selected_search_merged_after_self_enables_merged() -> None:
    assert _selected_search(_ns(), "abc") == SearchZones(
        False, False, frozenset({"completed"}), "abc")


def test_search_active_labels_zone_and_carries_prose(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dark mode inflight",
                  quest_id="liveid", base_branch="main")
    hits = _search_active(tmp_path, "dark")
    assert [h.quest_id for h in hits] == ["liveid"]
    assert (hits[0].status, hits[0].zone) == ("new", "active")
    assert hits[0].title == "Dark mode inflight"


def test_search_backlog_matches_seed_opening_body(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path, title="Flaky tracker", quest_id="bk1", base_branch="main",
        origin="idea", seed_opening="Flaky tracker\n\ntracking test_widget_timeout")
    # Term lives only in the description body, not the id or title.
    hits = _search_backlog(tmp_path, "test_widget_timeout")
    assert [h.quest_id for h in hits] == ["bk1"]
    assert (hits[0].status, hits[0].zone) == ("backlog", "backlog")
    assert "test_widget_timeout" in hits[0].content


def test_search_backlog_excludes_nonmatching(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path, title="Unrelated idea", quest_id="bk2", base_branch="main",
        origin="idea", seed_opening="Unrelated idea")
    assert _search_backlog(tmp_path, "zzznope") == []


def test_search_archived_filters_by_disposition_and_labels_zone(tmp_path: Path) -> None:
    _archive(tmp_path, "arccomp", "Dark archived merged", status="completed")
    _archive(tmp_path, "arcaband", "Dark archived abandoned", status="abandoned")
    hits = _search_archived(tmp_path, "dark", {"abandoned"}, None)
    assert [h.quest_id for h in hits] == ["arcaband"]   # completed excluded by status
    assert (hits[0].status, hits[0].zone) == ("abandoned", "history")


def _user_msg_line(text: str) -> str:
    return json.dumps({
        "type": "user_message",
        "payload": {"role": "user", "subtype": "reply",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n"


def _engine_prompt_line(text: str) -> str:
    return json.dumps({
        "type": "engine_message",
        "payload": {"role": "user", "subtype": "agent_prompt",
                    "content": [{"type": "text", "text": text}]},
    }) + "\n"


def test_search_active_matches_conversation_prose_only(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Quiet chat", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(_user_msg_line("please wire a 300s deadline soon"))
    # "300s" is in neither the id nor the title — only the conversation prose.
    hits = _search_active(tmp_path, "300s")
    assert [h.quest_id for h in hits] == ["liveid"]
    assert (hits[0].status, hits[0].zone) == ("new", "active")
    assert "300s" in hits[0].content


def test_search_active_excludes_nonhuman_user_message(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Quiet chat", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(_engine_prompt_line("cover-check injected prompt"))
    # The term lives only in a non-human (injected) user_message -> not searchable,
    # and it is in neither the id "liveid" nor the title "Quiet chat".
    assert _search_active(tmp_path, "cover-check") == []


def test_search_archived_completed_disposition_excludes_abandoned(tmp_path: Path) -> None:
    _archive(tmp_path, "alpha", "Dark mode", status="completed")
    _archive(tmp_path, "beta", "Dark login", status="abandoned")
    hits = _search_archived(tmp_path, "dark", {"completed"}, None)
    assert [h.quest_id for h in hits] == ["alpha"]      # abandoned excluded
    assert hits[0].title == "Dark mode"


def test_merged_after_keeps_only_fixes_that_landed_after(tmp_path: Path) -> None:
    sha_a, sha_b = _init_git_commits(tmp_path)          # b is a child of a
    _archive(tmp_path, "alpha", "Dark", status="completed", merge_commit=sha_b)
    # b is NOT an ancestor of a -> landed after -> kept (git exit 1).
    assert _merged_after(tmp_path, "alpha", sha_a) is True
    # b IS an ancestor of b (itself) -> landed at/before -> excluded (git exit 0).
    assert _merged_after(tmp_path, "alpha", sha_b) is False


def test_merged_after_excludes_missing_and_unverifiable(tmp_path: Path) -> None:
    sha_a, _sha_b = _init_git_commits(tmp_path)
    _archive(tmp_path, "nomerge", "Dark", status="completed", merge_commit=None)
    assert _merged_after(tmp_path, "nomerge", sha_a) is False   # no recorded merge SHA
    _archive(tmp_path, "alpha", "Dark", status="completed", merge_commit=sha_a)
    # Unknown ref -> git exits 128 (not 1) -> unverifiable -> excluded.
    assert _merged_after(tmp_path, "alpha", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") is False


def test_search_archived_applies_merged_after_to_completed(tmp_path: Path) -> None:
    sha_a, sha_b = _init_git_commits(tmp_path)
    _archive(tmp_path, "alpha", "Dark", status="completed", merge_commit=sha_b)
    assert [h.quest_id for h in
            _search_archived(tmp_path, "dark", {"completed"}, sha_a)] == ["alpha"]
    assert _search_archived(tmp_path, "dark", {"completed"}, sha_b) == []


class _ScanOnlyStore:
    """The two ops the backlog row builder needs, served with no disk at all."""

    def __init__(self, homes: list[HomeDigest]) -> None:
        self._homes = homes

    def scan_homes(self) -> list[HomeDigest]:
        return self._homes

    def backlog_readiness(self, depends_on: list[str]) -> Readiness:
        return Readiness(True, ())


def _backlog_digest(quest_id: str, title: str, seed: str) -> HomeDigest:
    return HomeDigest(
        quest_id=quest_id, quest_json=b"{}", state_json=None,
        meta={"id": quest_id, "title": title, "materialized": False,
              "origin": "backlog", "seed_opening": seed,
              "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []},
        last_iso="2026-06-11T00:00:00+00:00", has_user_message=False,
        prose="", intent_title=None, lock_live=False)


def test_search_backlog_takes_the_body_from_the_store_scan(
        monkeypatch, tmp_path: Path) -> None:
    # No quest home exists on disk, so a hit on a term that lives only in the
    # description body proves the body came from the scanned digest — the route a
    # sandboxed client gets from the daemon.
    store = _ScanOnlyStore([_backlog_digest(
        "bk9", "Flaky tracker", "Flaky tracker\n\ntracking test_widget_timeout")])
    monkeypatch.setattr("imp.cli.store.make_store", lambda control: store)
    hits = _search_backlog(tmp_path, "test_widget_timeout")
    assert [h.quest_id for h in hits] == ["bk9"]
    assert "test_widget_timeout" in hits[0].content
    assert hits[0].last_activity == "2026-06-11T00:00:00+00:00"


def _inflight(tmp_path: Path, quest_id: str, status: str = "new", *,
              waiting: bool = False, lock_live: bool = False) -> None:
    q = Quest.create(tmp_path, title=f"Dark {quest_id}", quest_id=quest_id,
                     base_branch="main")
    st = State.load(q.state_dir)
    st.status = status
    if waiting:
        st.halt_condition = "waiting"
    st.save(q.state_dir)
    if lock_live:
        (q.quest_dir / "lock").write_text(str(os.getpid()))


@pytest.mark.parametrize(("stored", "kw", "shown"), [
    ("paused", {}, "paused"),
    ("merge_blocked", {}, "merge_blocked"),
    ("error", {}, "error"),
    ("new", {"lock_live": True}, "running"),
    ("paused", {"waiting": True, "lock_live": True}, "waiting"),
])
def test_search_active_hit_carries_resolved_status(
        tmp_path: Path, stored: str, kw: dict, shown: str) -> None:
    _seed_control(tmp_path)
    _inflight(tmp_path, "darkq1", stored, **kw)
    hits = _search_active(tmp_path, "dark")
    assert [(h.quest_id, h.status, h.zone) for h in hits] == [("darkq1", shown, "active")]


@pytest.mark.parametrize("status", ["completed", "abandoned", "archived"])
def test_search_archived_hit_carries_stored_status(tmp_path: Path, status: str) -> None:
    _archive(tmp_path, "darkarc", "Dark archived", status=status)
    hits = _search_archived(tmp_path, "dark", {"completed", "abandoned", "archived"}, None)
    assert [(h.status, h.zone) for h in hits] == [(status, "history")]


@pytest.mark.parametrize(("status", "zone"), [
    ("running", "active"), ("waiting", "active"), ("paused", "active"),
    ("error", "active"), ("new", "active"), ("merge_blocked", "active"),
    ("backlog", "backlog"),
    ("completed", "history"), ("abandoned", "history"), ("archived", "history"),
])
def test_search_hit_zone_is_derived_from_status(status: str, zone: str) -> None:
    assert zone_of(status) == zone
    assert SearchHit("q", "T", status, "", None, None).zone == zone


def test_search_hit_stores_no_zone_field() -> None:
    names = [f.name for f in dataclasses.fields(SearchHit)]
    assert "zone" not in names and "status" in names
