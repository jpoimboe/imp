"""Archive version skew at _complete: defer the filing, keep the terminal status."""
from __future__ import annotations

import pytest

import imp.remote.session.attach as attach_mod
from imp.cli import narrative as nv
from imp.cli.main import RESUMABLE
from imp.cli.render_text import render_tree
from imp.engine import archive
from imp.engine import merge_queue as mq
from imp.engine.errors import ArchiveMismatch
from imp.engine.executor import RunResult, _complete
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, State
from imp.remote.session.discover import session_socket_path
from tests.conftest import _git, _seed_control

_MESSAGE = ("this quest finished but was not filed into the archive; "
            "imp files it on its next run")


def _skew(floor="data", stored=None, known=None):
    """An archive_quest stand-in that refuses the write the way check_data_floor does."""
    stored = archive._DATA_VERSION + 1 if stored is None else stored
    known = archive._DATA_VERSION if known is None else known

    def _raiser(*a, **k):
        raise ArchiveMismatch(floor=floor, stored=stored, known=known)

    return _raiser


def _merged_quest(tmp_path, monkeypatch, quest_id):
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="a", quest_id=quest_id, base_branch="main")
    (q.project_root / "f.txt").write_text("hi")
    _git(q.project_root, "add", "-A")
    _git(q.project_root, "commit", "-qm", "a")
    mq.enqueue_if_absent(tmp_path, quest_id, "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    state = State.load(q.state_dir)
    state.merge_commit = "deadbeef"        # merge already durable -> merged branch
    return q, state, JsonlEventLog(q.events_path)


def _done_quest(tmp_path, monkeypatch, quest_id):
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="x", quest_id=quest_id, base_branch="main")
    mq.enqueue_if_absent(tmp_path, quest_id, "main")
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)
    return q, State.load(q.state_dir), JsonlEventLog(q.events_path)   # merge_commit None


def _internal_errors(log):
    return [e for e in log.read()
            if e.get("type") == "error"
            and e.get("payload", {}).get("category") == "internal"]


def test_skew_on_the_merged_branch_finishes_clean(tmp_path, monkeypatch) -> None:
    q, state, log = _merged_quest(tmp_path, monkeypatch, "skma")
    monkeypatch.setattr(archive, "archive_quest", _skew())

    assert _complete(q, state, log) == RunResult.COMPLETED

    assert State.load(q.state_dir).status == "completed"
    assert (q.quest_dir / "quest.json").is_file()      # home whole for the sweep
    assert (q.state_dir / "state.json").is_file()
    assert q.events_path.is_file()
    types = [e.get("type") for e in log.read()]
    assert "workflow_paused" not in types
    assert _internal_errors(log) == []
    assert types.count("quest_merged") == 1


def test_skew_on_the_done_branch_finishes_clean(tmp_path, monkeypatch) -> None:
    q, state, log = _done_quest(tmp_path, monkeypatch, "skdo")
    monkeypatch.setattr(archive, "archive_quest", _skew())

    assert _complete(q, state, log) == RunResult.COMPLETED

    assert State.load(q.state_dir).status == "completed"
    assert (q.quest_dir / "quest.json").is_file()
    assert (q.state_dir / "state.json").is_file()
    assert q.events_path.is_file()
    types = [e.get("type") for e in log.read()]
    assert "workflow_paused" not in types
    assert _internal_errors(log) == []
    assert types.count("workflow_completed") == 1


def test_the_deferral_warning_payload_is_exact(tmp_path, monkeypatch) -> None:
    q, state, log = _merged_quest(tmp_path, monkeypatch, "skpa")
    monkeypatch.setattr(archive, "archive_quest", _skew())

    _complete(q, state, log)

    warnings = [e for e in log.read() if e.get("type") == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["payload"] == {
        "source": "archive_skew",
        "message": _MESSAGE,
        "detail": (f"archive data is at v{archive._DATA_VERSION + 1}; "
                   f"this imp knows v{archive._DATA_VERSION}"),
    }


def test_the_warning_detail_is_built_from_the_exceptions_fields(
    tmp_path, monkeypatch,
) -> None:
    q, state, log = _done_quest(tmp_path, monkeypatch, "skix")
    monkeypatch.setattr(archive, "archive_quest",
                        _skew(floor="index", stored=8, known=7))

    _complete(q, state, log)

    warnings = [e for e in log.read() if e.get("type") == "warning"]
    assert [w["payload"]["detail"] for w in warnings] == [
        "archive index is at v8; this imp knows v7"]


def test_a_deferred_quest_is_not_resumable(tmp_path, monkeypatch) -> None:
    q, state, log = _merged_quest(tmp_path, monkeypatch, "skre")
    monkeypatch.setattr(archive, "archive_quest", _skew())

    _complete(q, state, log)

    assert State.load(q.state_dir).status not in RESUMABLE   # no re-entry, no 2nd merge event


def test_scratch_is_still_reaped_when_the_archive_is_deferred(
    tmp_path, monkeypatch,
) -> None:
    q, state, log = _merged_quest(tmp_path, monkeypatch, "sksc")
    q.tmp_dir.mkdir(parents=True, exist_ok=True)
    (q.tmp_dir / "scratch").write_text("x")
    q.var_tmp_dir.mkdir(parents=True, exist_ok=True)
    (q.var_tmp_dir / "scratch").write_text("x")
    sock = session_socket_path("sksc")
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.write_bytes(b"")
    monkeypatch.setattr(archive, "archive_quest", _skew())

    _complete(q, state, log)

    assert not q.tmp_dir.exists()        # nothing else reaps these
    assert not q.var_tmp_dir.exists()
    assert not sock.exists()
    assert q.quest_dir.is_dir()          # ...but the home stays


def test_a_non_skew_archive_failure_still_propagates(tmp_path, monkeypatch) -> None:
    q, state, log = _merged_quest(tmp_path, monkeypatch, "skbo")

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(archive, "archive_quest", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        _complete(q, state, log)


def test_the_success_path_reaps_the_home_and_emits_no_warning(
    tmp_path, monkeypatch,
) -> None:
    from imp.engine.archive import read as archive_read
    q, state, log = _merged_quest(tmp_path, monkeypatch, "skok")

    assert _complete(q, state, log) == RunResult.COMPLETED

    assert not q.quest_dir.exists()
    home = archive_read.rehydrate_quest(q.control_root, q.id)     # home reaped: read it back
    events = JsonlEventLog(home / "events.jsonl").read()
    assert [e for e in events if e.get("type") == "warning"] == []


def test_the_deferral_warning_renders_as_one_warning_line_plus_its_detail(
    tmp_path, monkeypatch,
) -> None:
    q, state, log = _merged_quest(tmp_path, monkeypatch, "skrn")
    monkeypatch.setattr(archive, "archive_quest", _skew(stored=14, known=13))
    _complete(q, state, log)

    tree = nv.build_tree(log.read())      # read() is a generator: drain it once
    out = list(render_tree(tree, full=False, color=False, width=100))

    # endswith/index, not equality: a line may carry an indent and a gutter prefix.
    i = next(k for k, ln in enumerate(out) if ln.endswith(f"warning: {_MESSAGE}"))
    detail = out[i + 1]
    assert detail.endswith("archive data is at v14; this imp knows v13")
    assert detail.index("archive data") > out[i].index("warning:")   # detail is indented
    assert "error:" not in "\n".join(out)

    nodes: list = []

    def _walk(node) -> None:
        if isinstance(node, nv.ErrorNode):
            nodes.append(node)
        for child in getattr(node, "children", []):
            _walk(child)

    _walk(tree.root)
    assert [n.is_warning for n in nodes] == [True]    # warning role, not the error role
