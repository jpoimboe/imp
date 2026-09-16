"""bwrap --ro-bind enforces read-only; mode bits don't deny root or uid 0 in a user namespace."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.sandbox import sandbox_available
from imp.engine.state import State
from imp.paths import project_state_dir

_needs_bwrap = pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable")

# src/ on the child's PYTHONPATH so in-sandbox `python -m imp` resolves the
# working-tree package (mirrors conftest's subprocess PYTHONPATH pin).
_SRC = str(Path(__file__).resolve().parents[1] / "src")

LIVE = "livequest"
ARCH = "arch0099"
LIVE_MARK = "SPROCKETS"
ARCH_MARK = "FLYWHEEL"
SPEC_MARK = "SPECMARK"

# One entry per read command named in the spec, with substrings its output must
# carry — the archived quest among them, since the skip must not suppress it.
_READ_COMMANDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("list",), (ARCH, "Archived Quest")),
    (("list", "--all"), (ARCH, LIVE)),
    (("show", ARCH), ("Archived Quest", ARCH_MARK)),
    (("show", LIVE, "spec"), (SPEC_MARK,)),
    (("log", ARCH), ("Archived Quest", ARCH_MARK)),
    (("search", "sprockets"), (LIVE, LIVE_MARK)),
    (("events", ARCH), (ARCH, "2 events")),
    (("events", "search", "flywheel"), (ARCH, ARCH_MARK)),
)

# Below the blob floor the uniform expectation splits: index-only and live-only
# reads still answer, while every read of the archived record refuses.
_BELOW_FLOOR_OK: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("list",), (ARCH, "Archived Quest")),
    (("list", "--all"), (ARCH, LIVE)),
    (("show", LIVE, "spec"), (SPEC_MARK,)),
    (("search", "sprockets"), (LIVE, LIVE_MARK)),
)

_BELOW_FLOOR_REFUSED: tuple[tuple[str, ...], ...] = (
    ("show", ARCH),
    ("log", ARCH),
    ("events", ARCH),
    ("events", "search", "flywheel"),
    ("resume", ARCH),
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _events(marker: str) -> list[dict[str, object]]:
    return [
        {"id": "e1", "timestamp": "2026-06-01T00:00:01+00:00", "type": "user_message",
         "payload": {"role": "user", "subtype": "intent",
                     "content": [{"type": "text", "text": marker}]}},
        {"id": "e2", "timestamp": "2026-06-01T00:00:02+00:00", "type": "user_message",
         "payload": {"role": "user", "subtype": "reply",
                     "content": [{"type": "text", "text": marker}]}},
    ]


def _write_quest(
    control: Path, qid: str, *, title: str, marker: str,
    status: str = "new", spec: str | None = None,
) -> Path:
    d = project_state_dir(control) / "quests" / qid
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "quest.json").write_text(json.dumps({
        "id": qid, "title": title, "base_branch": "main",
        "project_root": str(control), "layout_version": LAYOUT_VERSION,
        "created_at": "2026-06-01T00:00:00+00:00",
        "materialized": True, "origin": "draft", "depends_on": []}))
    State(id=qid, step="implement", step_attempt=0, status=status).save(d / "state")
    with (d / "events.jsonl").open("w", encoding="utf-8") as f:
        for event in _events(marker):
            f.write(json.dumps(event) + "\n")
    if spec is not None:
        (d / "artifacts" / "spec").mkdir(parents=True, exist_ok=True)
        (d / "artifacts" / "spec" / f"{qid}.md").write_text(spec)
    return d


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """A git-repo control root whose state dir resolves through IMP_HOME/projects,
    holding one live quest with a spec and one archived quest whose live home is
    gone. Returns (repo, home, state)."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("IMP_HOME", str(home))
    monkeypatch.delenv("IMP_STATE_DIR", raising=False)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".imp/\n")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _write_quest(repo, LIVE, title="Live Quest", marker=LIVE_MARK,
                 spec=f"# spec\n\n{SPEC_MARK} body\n")
    archived = _write_quest(repo, ARCH, title="Archived Quest", marker=ARCH_MARK,
                            status="merged")
    archive.archive_quest(
        repo, ARCH, events_path=archived / "events.jsonl",
        quest_json_path=archived / "quest.json",
        state_json_path=archived / "state" / "state.json")
    shutil.rmtree(archived)
    return repo, home, project_state_dir(repo)


def _run_readonly(
    base: Path, repo: Path, home: Path, *args: str,
) -> subprocess.CompletedProcess[str]:
    """`imp <args>` in `repo`, with IMP_HOME/projects bound read-only and neither
    IMP_STATE_DIR nor a session socket pinning the read."""
    child_tmp = base / "childtmp"
    child_tmp.mkdir(exist_ok=True)
    # no IMP_STATE_DIR, IMP_SESSION_SOCKET cleared: documented cross-project read form
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(child_tmp),
        "PYTHONPATH": _SRC,
        "IMP_HOME": str(home),
        "IMP_SESSION_SOCKET": "",
        "NO_COLOR": "1",
        "COLUMNS": "100",
    }
    argv = [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        "--bind", str(base), str(base),
        "--ro-bind", str(home / "projects"), str(home / "projects"),
        "--chdir", str(repo),
        sys.executable, "-m", "imp", *args,
    ]
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)


def _run_writable(repo: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """`imp <args>` in `repo` with no sandbox — the writable counterpart of
    `_run_readonly`, which no longer diverges now that nothing heals the marker."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONPATH": _SRC,
        "IMP_HOME": str(home),
        "IMP_SESSION_SOCKET": "",
        "NO_COLOR": "1",
        "COLUMNS": "100",
    }
    return subprocess.run(
        [sys.executable, "-m", "imp", *args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=120)


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        out[rel] = "d" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _assert_every_read_command_is_clean(
    base: Path, repo: Path, home: Path, state: Path,
) -> None:
    before = _snapshot(state)
    for args, expected in _READ_COMMANDS:
        proc = _run_readonly(base, repo, home, *args)
        label = "imp " + " ".join(args)
        assert proc.returncode == 0, f"{label} rc={proc.returncode}\n{proc.stderr}"
        for needle in expected:
            assert needle in proc.stdout, f"{label} missing {needle!r}\n{proc.stdout}"
    # Harness guard: the ro bind makes writes impossible; this fails only if that bind is dropped.
    assert _snapshot(state) == before


@_needs_bwrap
def test_read_commands_work_against_a_healthy_read_only_state_dir(
    tmp_path: Path, project: tuple[Path, Path, Path],
) -> None:
    repo, home, state = project

    _assert_every_read_command_is_clean(tmp_path, repo, home, state)


@_needs_bwrap
def test_read_commands_work_when_the_index_schema_stamp_is_stale(
    tmp_path: Path, project: tuple[Path, Path, Path],
) -> None:
    repo, home, state = project
    conn = sqlite3.connect(archive.index_path(repo))
    try:
        conn.execute("PRAGMA user_version = 4")   # stamp behind, schema untouched
        conn.commit()
    finally:
        conn.close()

    _assert_every_read_command_is_clean(tmp_path, repo, home, state)


def _assert_below_floor_split(
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Index-only and live-only reads answer; every archived-record read refuses
    with one `imp:` line, exit 2, no traceback."""
    for args, expected in _BELOW_FLOOR_OK:
        proc = run(*args)
        label = "imp " + " ".join(args)
        assert proc.returncode == 0, f"{label} rc={proc.returncode}\n{proc.stderr}"
        for needle in expected:
            assert needle in proc.stdout, f"{label} missing {needle!r}\n{proc.stdout}"
    for args in _BELOW_FLOOR_REFUSED:
        proc = run(*args)
        label = "imp " + " ".join(args)
        assert proc.returncode == 2, f"{label} rc={proc.returncode}\n{proc.stderr}"
        lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1, f"{label} stderr not one line\n{proc.stderr}"
        assert lines[0].startswith("imp: "), f"{label} stderr {lines[0]!r}"
        assert "Traceback" not in proc.stderr, f"{label} traceback\n{proc.stderr}"


@_needs_bwrap
def test_below_floor_reads_split_on_a_read_only_archive(
    tmp_path: Path, project: tuple[Path, Path, Path],
) -> None:
    repo, home, state = project
    archive.data_version_path(repo).unlink()
    before = _snapshot(state)

    _assert_below_floor_split(lambda *args: _run_readonly(tmp_path, repo, home, *args))

    # Harness guard: the ro bind makes writes impossible; this fails only if that bind is dropped.
    assert _snapshot(state) == before


def test_below_floor_reads_split_on_a_writable_archive(
    tmp_path: Path, project: tuple[Path, Path, Path],
) -> None:
    """Nothing heals the marker any more, so a writable archive refuses exactly
    what a read-only one does."""
    repo, home, _state = project
    archive.data_version_path(repo).unlink()

    _assert_below_floor_split(lambda *args: _run_writable(repo, home, *args))

    assert not archive.data_version_path(repo).exists()
