"""The engine harvests change_dir/artifacts/** to refs/imp/artifacts/<id>."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from imp.engine import executor
from imp.engine.gates import RuleMatch
from imp.engine.types import Continue


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _change(root: Path, quest_id: str) -> SimpleNamespace:
    # The harvest helper only reads .control_root, .change_dir and .id.
    return SimpleNamespace(
        control_root=root,
        change_dir=root / ".imp" / "changes" / quest_id,
        id=quest_id,
    )


def test_harvest_journals_disk_artifacts_and_guards_redundant_commits(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    ch = _change(tmp_path, "feat")
    spec = ch.change_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / "feat.md").write_text("# spec\n")

    executor._harvest_artifacts(ch, SimpleNamespace(name="spec-finalizer"))
    assert _git(tmp_path, "show",
                "refs/imp/artifacts/feat:spec/feat.md").stdout == "# spec\n"
    # Commit message derives from the step name (no hardcoded kind).
    assert _git(tmp_path, "log", "-1", "--format=%s",
                "refs/imp/artifacts/feat").stdout.strip() == "artifact: spec-finalizer"
    head1 = _git(tmp_path, "rev-parse", "refs/imp/artifacts/feat").stdout.strip()

    # A later step that did not change artifacts/ adds NO new journal commit.
    executor._harvest_artifacts(ch, SimpleNamespace(name="plan-reviewer"))
    assert _git(tmp_path, "rev-parse",
                "refs/imp/artifacts/feat").stdout.strip() == head1


def test_harvest_is_noop_when_artifacts_dir_absent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ch = _change(tmp_path, "bare")
    # No artifacts/ dir exists (e.g. an early branch step).
    executor._harvest_artifacts(ch, SimpleNamespace(name="branch"))
    assert _git(tmp_path, "rev-parse", "--verify", "--quiet",
                "refs/imp/artifacts/bare").returncode != 0


class _Log:
    """Minimal EventLog stand-in: records each append as a dict."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, *, agent_id=None, type, payload):
        ev = {"agent_id": agent_id, "type": type, "payload": payload}
        self.events.append(ev)
        return ev


def _finish_change(root: Path, quest_id: str) -> SimpleNamespace:
    """A change with a non-empty artifacts/ dir so _harvest_artifacts reaches
    artifact_commit."""
    ch = _change(root, quest_id)
    spec = ch.change_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / f"{quest_id}.md").write_text("# spec\n")
    return ch


def _match() -> RuleMatch:
    return RuleMatch(action=Continue(), rule=None, var_mutations={})


def test_finish_survives_harvest_failure_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    ch = _finish_change(tmp_path, "feat")

    boom = subprocess.CalledProcessError(
        returncode=128, cmd=["git", "commit-tree"],
        stderr="fatal: bad object refs/imp/artifacts/feat\n")

    def _raise(*_a, **_k):
        raise boom

    monkeypatch.setattr("imp.engine.worktree.artifact_commit", _raise)

    log = _Log()
    state = SimpleNamespace(route_label="stale")
    action, feedback, muts = executor._finish(
        log, SimpleNamespace(name="w", kind="agent"), ch, _match(), state)

    # The step still completes: the Continue action is returned unchanged.
    assert isinstance(action, Continue)
    assert feedback is None
    assert muts == {}

    types = [e["type"] for e in log.events]
    assert "step_completed" in types
    # The warning is emitted, and it PRECEDES step_completed.
    assert types.index("warning") < types.index("step_completed")

    w = log.events[types.index("warning")]["payload"]
    assert w["source"] == "artifact_harvest"
    assert w["message"].endswith(" — fatal: bad object refs/imp/artifacts/feat")
    assert "fatal: bad object" in w["detail"]["stderr_tail"]


def test_finish_emits_warning_when_harvest_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The plan-2 warn callback (unreadable ref -> new lineage) is threaded through
    # _harvest_artifacts and surfaces as a `warning` engine event.
    _init_repo(tmp_path)
    ch = _finish_change(tmp_path, "feat")

    lineage_msg = (
        "artifact ref refs/imp/artifacts/feat unreadable; "
        "starting new artifact lineage")

    def _warns(*_a, **k):
        k["warn"](lineage_msg)
        return True

    monkeypatch.setattr("imp.engine.worktree.artifact_commit", _warns)

    log = _Log()
    executor._finish(
        log, SimpleNamespace(name="w", kind="agent"), ch, _match(),
        SimpleNamespace(route_label=None))

    warnings = [e["payload"] for e in log.events if e["type"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["source"] == "artifact_harvest"
    assert warnings[0]["message"] == lineage_msg
    assert warnings[0]["detail"] == {}
    assert any(e["type"] == "step_completed" for e in log.events)


def test_finish_propagates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # anyio-on-asyncio's cancelled class IS asyncio.CancelledError, a
    # BaseException subclass — so `except Exception` in _finish must let it
    # propagate. Use it directly: anyio.get_cancelled_exc_class() resolves the
    # backend via sniffio and only works inside a running event loop, so it
    # cannot be called from this plain (sync) test.
    _init_repo(tmp_path)
    ch = _finish_change(tmp_path, "feat")

    def _cancel(*_a, **_k):
        raise asyncio.CancelledError()

    monkeypatch.setattr("imp.engine.worktree.artifact_commit", _cancel)

    log = _Log()
    with pytest.raises(asyncio.CancelledError):
        executor._finish(
            log, SimpleNamespace(name="w", kind="agent"), ch, _match(),
            SimpleNamespace(route_label=None))

    # A cancellation must NOT be swallowed into a warning, and the step must NOT
    # complete.
    assert not any(e["type"] == "warning" for e in log.events)
    assert not any(e["type"] == "step_completed" for e in log.events)
