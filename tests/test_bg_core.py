# tests/test_bg_core.py
from __future__ import annotations

import json
from pathlib import Path

from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path, agent_id: str = "step:plan/coder") -> ToolContext:
    run_dir = tmp_path / ".imp" / "change"
    run_dir.mkdir(parents=True, exist_ok=True)
    return ToolContext(project_root=tmp_path,
                       events_path=run_dir / "events.jsonl", agent_id=agent_id)


# --- scratch dir namespacing -------------------------------------------------

def test_bg_dir_is_per_invocation_and_hash_disambiguated(tmp_path: Path) -> None:
    from imp.agents.tools.bg import _bg_dir
    a = _bg_dir(_ctx(tmp_path, "step:review/final"))
    b = _bg_dir(_ctx(tmp_path, "researcher-1"))
    assert a != b
    assert a.parent == b.parent == tmp_path / ".imp" / "change" / "bg"
    # sanitizer is many-to-one; the hash suffix keeps colliding ids distinct
    c = _bg_dir(_ctx(tmp_path, "step.review.final"))
    assert a != c


# --- tombstone file tolerance -----------------------------------------------

def test_unparseable_tombstone_file_reads_as_empty(tmp_path: Path) -> None:
    from imp.agents.tools.bg import _load_tombstones
    p = tmp_path / "tombstones.json"
    p.write_text("{torn-mid-wri")
    assert _load_tombstones(p) == {}
    assert _load_tombstones(tmp_path / "absent.json") == {}
    p.write_text(json.dumps(["not", "a", "dict"]))
    assert _load_tombstones(p) == {}


# --- counter seeding ----------------------------------------------------------

def test_seed_from_tombstone_file(tmp_path: Path) -> None:
    from imp.agents.tools.bg import _bg_dir, _seed_counter
    ctx = _ctx(tmp_path)
    d = _bg_dir(ctx)
    d.mkdir(parents=True)
    (d / "tombstones.json").write_text(json.dumps(
        {"bg1": {"status": "killed"}, "bg3": {"status": "exited"}}))
    _seed_counter(ctx)
    assert ctx.next_bg_id() == "bg4"


def test_seed_from_stream_filenames_after_ungraceful_death(tmp_path: Path) -> None:
    """No tombstone file (teardown never ran) — seeding must still get past the
    filename-derived ids, or the first post-resume spawn re-aliases bg1."""
    from imp.agents.tools.bg import _bg_dir, _seed_counter
    ctx = _ctx(tmp_path)
    d = _bg_dir(ctx)
    d.mkdir(parents=True)
    (d / "bg1.out").write_bytes(b"x")
    (d / "bg2.err").write_bytes(b"")
    _seed_counter(ctx)
    assert ctx.next_bg_id() == "bg3"


def test_seed_noop_without_dir_or_with_advanced_counter(tmp_path: Path) -> None:
    from imp.agents.tools.bg import _seed_counter
    ctx = _ctx(tmp_path)
    _seed_counter(ctx)                  # dir absent
    assert ctx.next_bg_id() == "bg1"
    _seed_counter(ctx)                  # counter already advanced: no reseed
    assert ctx.next_bg_id() == "bg2"


# --- UTF-8 page-boundary splitter ---------------------------------------------

def test_utf8_tail_holds_back_valid_incomplete_prefix() -> None:
    from imp.agents.tools.bg import _split_incomplete_utf8_tail
    full = "中".encode()                       # 3 bytes: e4 b8 ad
    emit, held = _split_incomplete_utf8_tail(b"abc" + full[:2])
    assert emit == b"abc" and held == full[:2]
    emit, held = _split_incomplete_utf8_tail(b"abc" + full[:1])
    assert emit == b"abc" and held == full[:1]
    # 4-byte sequence cut at 3
    emoji = "🎉".encode()
    emit, held = _split_incomplete_utf8_tail(emoji[:3])
    assert emit == b"" and held == emoji[:3]


def test_utf8_tail_emits_complete_ascii_and_garbage() -> None:
    from imp.agents.tools.bg import _split_incomplete_utf8_tail
    assert _split_incomplete_utf8_tail(b"hello") == (b"hello", b"")
    assert _split_incomplete_utf8_tail("中".encode()) == ("中".encode(), b"")
    # invalid lead byte / bare continuations: NOT a prefix — emit for replace
    assert _split_incomplete_utf8_tail(b"ab\xff") == (b"ab\xff", b"")
    assert _split_incomplete_utf8_tail(b"\x80\x80\x80\x80") == (b"\x80\x80\x80\x80", b"")
    assert _split_incomplete_utf8_tail(b"") == (b"", b"")
