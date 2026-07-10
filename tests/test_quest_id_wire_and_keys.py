import json
from pathlib import Path

from tests.conftest import _seed_control


def test_palette_node_colors_keyed_on_quest_id() -> None:
    from imp.cli import palette as pal
    assert "quest_id" in pal.NODE_COLORS
    assert "change_id" not in pal.NODE_COLORS


def test_daemon_hello_carries_quest_id() -> None:
    from imp.remote.daemon.protocol import make_hello, parse_hello
    line = make_hello("kobe47", "Title")
    qid, title, auto = parse_hello(line)
    assert qid == "kobe47"
    assert json.loads(line)["quest_id"] == "kobe47"
    assert "change_id" not in json.loads(line)


def test_session_attach_encodes_quest_id() -> None:
    from imp.remote.session.protocol import Attach, encode_attach
    assert json.loads(encode_attach("kobe47", "T"))["quest_id"] == "kobe47"
    assert "quest_id" in Attach.__annotations__


def test_merge_queue_entry_keyed_on_quest_id(tmp_path: Path) -> None:
    from imp.engine import merge_queue as mq
    _seed_control(tmp_path)
    mq.enqueue_if_absent(tmp_path, "kobe47", "main")
    entries = mq.read(tmp_path)
    assert entries[0]["quest_id"] == "kobe47"
    assert "change_id" not in entries[0]


def test_archive_index_deps_column_is_quest_id(tmp_path: Path) -> None:
    from imp.engine.archive.index import connect
    conn = connect(tmp_path / "index.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(deps)")}
    assert "quest_id" in cols
    assert "change_id" not in cols


def test_discover_exposes_quest_info_and_list_quests() -> None:
    from imp.remote.daemon import discover
    names = set(vars(discover))
    assert "QuestInfo" in names
    assert "list_quests" in names
    assert "Change" "Info" not in names    # split literal so the rename sweep can't rewrite this guard
    assert "list_chang" "es" not in names  # split literal so the rename sweep can't rewrite this guard


def test_build_record_uses_quest_meta_key() -> None:
    from imp.engine.archive.record import build_record
    rec = build_record({"id": "kobe47", "title": "T", "base_branch": "main"}, {}, "deadbeef")
    assert "quest_meta" in rec
    assert "change_" "meta" not in rec  # split literal so rename sweep can't rewrite this guard
    assert rec["quest_meta"]["id"] == "kobe47"


def test_archive_read_exposes_read_quest_json() -> None:
    from imp.engine.archive import read as ar
    assert hasattr(ar, "read_quest_json")
    assert not hasattr(ar, "read_change_" "json")  # split literal: change_json broken so rename sweep can't rewrite this guard


def test_archive_index_has_quests_table(tmp_path: Path) -> None:
    from imp.engine.archive.index import connect
    conn = connect(tmp_path / "index.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "quests" in names
    assert "chang" "es" not in names  # split literal: dodges the rename sweep + straggler grep
