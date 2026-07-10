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
