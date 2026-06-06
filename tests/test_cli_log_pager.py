"""Tests for `imp log` output (src/imp/cli/main.py::_cmd_log).

The pager was removed: `imp log` now prints rendered lines straight to
stdout with no subprocess, and there is no `--no-pager` flag. These tests
lock in that behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.cli.main import main


def _make_change_worktree(control: Path, slug: str, title: str) -> Path:
    """Create a minimal on-disk worktree for a Change under control/.imp/worktrees/<slug>."""
    wt = control / ".imp" / "worktrees" / slug
    change_dir = wt / ".imp" / "change"
    change_dir.mkdir(parents=True)
    (change_dir / "change.json").write_text(
        json.dumps({"id": slug, "title": title, "base_branch": "main"}),
    )
    (change_dir / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"slug": slug, "title": title},
        }) + "\n"
        # A step renders the phase name; the slug/title banner no longer renders,
        # so the phase is what proves the narrative reached stdout.
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": "spec",
                        "model": "m", "provider": "anthropic"},
        }) + "\n",
    )
    return wt


def test_log_prints_rendered_lines_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["log", "<slug>"])` writes the narrative to stdout directly —
    no pager subprocess. The rendered phase "spec" appears in captured stdout."""
    import imp.cli.main as m

    slug = "my-smoke"
    _make_change_worktree(tmp_path, slug, "smoke")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", slug])
    assert rc == 0
    out = capsys.readouterr().out
    assert "spec" in out


def test_log_no_pager_flag_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `--no-pager` flag was removed with the pager. argparse rejects it
    with a SystemExit(2). Catches an accidental re-add of the flag."""
    import imp.cli.main as m

    slug = "my-smoke"
    _make_change_worktree(tmp_path, slug, "smoke")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    with pytest.raises(SystemExit) as exc:
        main(["log", "--no-pager", slug])
    assert exc.value.code == 2
