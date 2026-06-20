from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from imp.cli import main
from imp.engine import worktree
from imp.engine.archive.read import archived_ids


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _journal_one(control: Path, cid: str) -> None:
    stage = control / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "events.jsonl").write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{}}\n')
    (stage / "change.json").write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    (stage / "state.json").write_text(json.dumps({
        "id": cid, "status": "completed", "step": "finalize"}))
    worktree.journal_commit(control, cid, {
        "events.jsonl": stage / "events.jsonl",
        "change.json": stage / "change.json",
        "state.json": stage / "state.json"})


def _journal_ref_exists(control: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/imp/journal"],
        cwd=control, capture_output=True, text=True)
    return proc.returncode == 0


def test_migrate_journal_confirmed_imports_and_retires(
    tmp_path: Path, monkeypatch,
) -> None:
    _git_init(tmp_path)
    _journal_one(tmp_path, "alpha")
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)

    rc = main._cmd_migrate_journal(
        argparse.Namespace(yes=False), input_fn=lambda _p: "y", interactive=True)

    assert rc == 0
    assert archived_ids(tmp_path) == ["alpha"]   # served from the index now
    assert not _journal_ref_exists(tmp_path)      # ref retired


def test_migrate_journal_declined_leaves_ref_intact(
    tmp_path: Path, monkeypatch,
) -> None:
    _git_init(tmp_path)
    _journal_one(tmp_path, "alpha")
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)

    rc = main._cmd_migrate_journal(
        argparse.Namespace(yes=False), input_fn=lambda _p: "n", interactive=True)

    assert rc == 1
    assert _journal_ref_exists(tmp_path)          # left untouched


def test_migrate_journal_empty_ref_is_a_noop(tmp_path: Path, monkeypatch) -> None:
    _git_init(tmp_path)
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)

    # input_fn must never be consulted: there is nothing to confirm.
    def _boom(_prompt: str) -> str:
        raise AssertionError("must not prompt when the journal is empty")

    rc = main._cmd_migrate_journal(argparse.Namespace(yes=False), input_fn=_boom)
    assert rc == 0


def test_migrate_journal_keeps_ref_when_a_change_cannot_import(
    tmp_path: Path, monkeypatch,
) -> None:
    _git_init(tmp_path)
    _journal_one(tmp_path, "alpha")
    # A second journaled id with only events.jsonl (no change.json/state.json):
    # import_change_runtime returns False for it, so it is silently skipped.
    incomplete = tmp_path / "stage" / "events.jsonl"
    incomplete.parent.mkdir(parents=True, exist_ok=True)
    incomplete.write_text('{"id":"e0","parent_id":null,"agent_id":null,'
                          '"timestamp":"2026-06-01T00:00:01+00:00",'
                          '"type":"change_created","payload":{}}\n')
    worktree.journal_commit(tmp_path, "beta", {"events.jsonl": incomplete})
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)

    def _no_prompt(_p: str) -> str:
        raise AssertionError("must not prompt when --yes is given")

    rc = main._cmd_migrate_journal(
        argparse.Namespace(yes=True), input_fn=_no_prompt, interactive=True)

    # Not every Change imported, so the ref is preserved and the run reports failure.
    assert rc == 1
    assert _journal_ref_exists(tmp_path)


def test_migrate_journal_yes_skips_prompt(tmp_path: Path, monkeypatch) -> None:
    _git_init(tmp_path)
    _journal_one(tmp_path, "alpha")
    monkeypatch.setattr(main, "_control_root", lambda: tmp_path)

    def _boom(_p: str) -> str:
        raise AssertionError("must not prompt when --yes is given")

    rc = main._cmd_migrate_journal(
        argparse.Namespace(yes=True), input_fn=_boom, interactive=True)
    assert rc == 0
    assert not _journal_ref_exists(tmp_path)
