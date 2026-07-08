"""Tests for the shell scripts shipped in the default workflow.

These run the actual scripts as subprocesses with controlled env vars and
working directories — catches regressions in the dogfood path that pure
unit tests miss.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir


def _scripts_dir() -> Path:
    return bundled_workflow_dir("imp") / "scripts"


def _run(script: str, *args: str, env_extra: dict[str, str] | None = None,
         env_remove: tuple[str, ...] = (), cwd: Path | None = None,
         ) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in env_remove:
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(_scripts_dir() / script), *args],
        env=env, cwd=cwd, capture_output=True, text=True, check=False,
    )


# ── run-verify.sh: tiered gate with per-tier tree-SHA skip cache ────────────


def _git_init(project: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)


def _git_commit_all(project: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=project,
                   env={**os.environ, "GIT_EDITOR": "true"}, check=True)


def test_run_verify_fast_command_succeeds(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": "true",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 0, proc.stderr


def test_run_verify_fast_command_fails(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": "false",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode != 0


@pytest.mark.parametrize("tier,cmd_var,timeout_var,key", [
    ("fast", "IMP_TEST_CMD_FAST", "IMP_TEST_CMD_FAST_TIMEOUT", "test_cmd_fast_timeout"),
    ("slow", "IMP_TEST_CMD_SLOW", "IMP_TEST_CMD_SLOW_TIMEOUT", "test_cmd_slow_timeout"),
])
def test_run_verify_timeout_exits_124_with_per_tier_diagnosis(
    tmp_path: Path, tier: str, cmd_var: str, timeout_var: str, key: str,
) -> None:
    # Exit 124 (GNU timeout) on overrun; the diagnosis must name THIS tier's
    # *_timeout config key so the human raises the right ceiling.
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", tier, env_extra={
        "IMP_PROJECT_ROOT": str(project), cmd_var: "sleep 5",
        timeout_var: "1", "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 124, proc.stderr
    assert key in proc.stdout
    assert "gate ceiling" in proc.stdout
    assert key not in proc.stderr


def test_run_verify_no_command_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast",
                env_extra={"IMP_PROJECT_ROOT": str(project)},
                env_remove=("IMP_TEST_CMD_FAST",))
    assert proc.returncode == 0, proc.stderr
    assert "no test command configured" in proc.stdout


def test_run_verify_empty_command_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": ""})
    assert proc.returncode == 0
    assert "no test command configured" in proc.stdout


def test_run_verify_slow_both_commands_unset_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "slow",
                env_extra={"IMP_PROJECT_ROOT": str(project)},
                env_remove=("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW"))
    assert proc.returncode == 0, proc.stderr
    assert "no test command configured" in proc.stdout


def test_run_verify_slow_uses_slow_command(tmp_path: Path) -> None:
    # slow tier runs IMP_TEST_CMD_SLOW, NOT IMP_TEST_CMD_FAST, when slow is set.
    project = tmp_path / "proj"
    project.mkdir()
    fast_marker = project / "fast_ran"
    slow_marker = project / "slow_ran"
    proc = _run("run-verify.sh", "slow", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": f'sh -c "echo f > {fast_marker}"',
        "IMP_TEST_CMD_SLOW": f'sh -c "echo s > {slow_marker}"',
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 0, proc.stderr
    assert slow_marker.exists()
    assert not fast_marker.exists()   # fast command never ran under the slow tier


def test_run_verify_slow_falls_back_to_fast_command_when_slow_empty(
    tmp_path: Path,
) -> None:
    # slow tier with IMP_TEST_CMD_SLOW unset runs IMP_TEST_CMD_FAST (slow ⊇ fast).
    project = tmp_path / "proj"
    project.mkdir()
    marker = project / "ran"
    proc = _run("run-verify.sh", "slow", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": f'sh -c "echo ok > {marker}"',
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")},
        env_remove=("IMP_TEST_CMD_SLOW",))
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text().strip() == "ok"   # fast command executed under slow tier


def test_run_verify_slow_timeout_default_when_unset(tmp_path: Path) -> None:
    # No IMP_TEST_CMD_SLOW_TIMEOUT => script applies its 300s default; sleep 2
    # would exit 124 if the default were accidentally set to <= 1s.
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "slow", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_SLOW": "sleep 2",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")},
        env_remove=("IMP_TEST_CMD_SLOW_TIMEOUT",))
    assert proc.returncode == 0, proc.stderr
    assert "gate ceiling" not in proc.stdout


def test_fast_and_slow_skip_caches_are_independent(tmp_path: Path) -> None:
    # A green `fast` run records last_green_tree_fast; a `slow` run on the SAME
    # tree must still RUN (its own cache is empty) — the tiers never share a cache.
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    change_dir = project / ".imp" / "change"
    fast_counter = project / "fast_count"
    slow_counter = project / "slow_count"

    fast = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": f'sh -c "echo x >> {fast_counter}"',
        "IMP_CHANGE_DIR": str(change_dir)})
    assert fast.returncode == 0, fast.stderr
    assert fast_counter.read_text().count("x") == 1

    slow = _run("run-verify.sh", "slow", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_SLOW": f'sh -c "echo y >> {slow_counter}"',
        "IMP_CHANGE_DIR": str(change_dir)})
    assert slow.returncode == 0, slow.stderr
    assert slow_counter.read_text().count("y") == 1   # slow ran despite fast's green cache
    assert "skipped" not in slow.stdout
    assert (change_dir / "last_green_tree_fast").exists()
    assert (change_dir / "last_green_tree_slow").exists()


def test_run_verify_fast_skips_on_unchanged_tree(tmp_path: Path) -> None:
    # First green fast run records HEAD's tree; a second fast run at the SAME tree
    # skips (does NOT re-run); a new commit re-runs.
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    counter = project / "count"
    cmd = f'sh -c "echo x >> {counter}"'

    first = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": cmd,
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert first.returncode == 0
    assert counter.read_text().count("x") == 1

    second = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": cmd,
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert second.returncode == 0
    assert "fast gate skipped — tree unchanged since last green run" in second.stdout
    assert counter.read_text().count("x") == 1   # command NOT re-run

    (project / "a.txt").write_text("2")
    _git_commit_all(project, "c2")
    third = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": cmd,
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert third.returncode == 0
    assert counter.read_text().count("x") == 2   # tree changed => re-ran


def test_two_fast_invocations_share_tree_skip_cache(tmp_path: Path) -> None:
    # Two fast-tier invocations at the same tree share the last_green_tree_fast
    # cache: the second fast invocation at the unchanged tree skips.
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    counter = project / "count"
    cmd = f'sh -c "echo x >> {counter}"'

    verify = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": cmd,
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert verify.returncode == 0, verify.stderr
    assert counter.read_text().count("x") == 1

    second_fast = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": cmd,
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert second_fast.returncode == 0, second_fast.stderr
    assert "fast gate skipped — tree unchanged since last green run" in second_fast.stdout
    assert counter.read_text().count("x") == 1   # second fast invocation did NOT re-run


def test_run_verify_writes_per_tier_cache_to_change_dir(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    home = tmp_path / "home"
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": "true",
        "IMP_CHANGE_DIR": str(home)})
    assert proc.returncode == 0, proc.stderr
    assert (home / "last_green_tree_fast").exists()
    assert not (home / "last_green_tree_slow").exists()   # slow tier untouched


def test_run_verify_unknown_tier_errors(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "bogus", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": "true",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 2
    assert "unknown tier" in proc.stderr


def test_run_verify_no_tier_arg_errors(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": "true",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode != 0
    assert "usage" in proc.stderr


def test_run_verify_merges_gate_stderr_into_stdout(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": "echo to-out; echo to-err >&2; exit 1",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode != 0
    assert "to-out" in proc.stdout
    assert "to-err" in proc.stdout          # gate stderr merged onto stdout
    assert "to-err" not in proc.stderr


def test_run_verify_failure_tail_is_last_on_stdout(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": "echo the-failure-line; exit 1",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 1
    assert "the-failure-line" in proc.stdout
    assert "gate failed (exit 1)" in proc.stdout
    # the tail content is reprinted AFTER the summary header, so the lean reveal lands on it
    assert proc.stdout.index("gate failed (exit 1)") < proc.stdout.rindex("the-failure-line")


def test_run_verify_timeout_guidance_is_last_line_block_on_stdout(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": "echo early-output; sleep 5",
        "IMP_TEST_CMD_FAST_TIMEOUT": "1",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 124
    assert "early-output" in proc.stdout
    assert "test_cmd_fast_timeout" in proc.stdout
    assert "gate ceiling" in proc.stdout
    assert any("test_cmd_fast_timeout" in ln for ln in proc.stdout.splitlines()[-6:])
    assert proc.stdout.index("early-output") < proc.stdout.index("test_cmd_fast_timeout")
    assert "test_cmd_fast_timeout" not in proc.stderr


def test_run_verify_success_summary_is_silent(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project),
        "IMP_TEST_CMD_FAST": "echo ran-ok; true",
        "IMP_CHANGE_DIR": str(project / ".imp" / "change")})
    assert proc.returncode == 0
    assert "ran-ok" in proc.stdout
    assert "gate ceiling" not in proc.stdout
    assert "gate failed" not in proc.stdout


def _head_tree(project: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=project,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_run_verify_slow_records_verified_tree_on_pass(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    change_dir = project / ".imp" / "change"
    proc = _run("run-verify.sh", "slow", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_SLOW": "true",
        "IMP_CHANGE_DIR": str(change_dir)})
    assert proc.returncode == 0, proc.stderr
    tree = _head_tree(project)
    assert (change_dir / "last_verified_tree_slow").read_text().strip() == tree
    assert (change_dir / "last_green_tree_slow").read_text().strip() == tree


def test_run_verify_slow_records_verified_tree_on_fail(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    change_dir = project / ".imp" / "change"
    proc = _run("run-verify.sh", "slow", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_SLOW": "false",
        "IMP_CHANGE_DIR": str(change_dir)})
    assert proc.returncode != 0
    tree = _head_tree(project)
    assert (change_dir / "last_verified_tree_slow").read_text().strip() == tree
    assert not (change_dir / "last_green_tree_slow").exists()   # red => no green stamp


def test_run_verify_fast_does_not_record_verified_tree_slow(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    change_dir = project / ".imp" / "change"
    proc = _run("run-verify.sh", "fast", env_extra={
        "IMP_PROJECT_ROOT": str(project), "IMP_TEST_CMD_FAST": "true",
        "IMP_CHANGE_DIR": str(change_dir)})
    assert proc.returncode == 0, proc.stderr
    assert not (change_dir / "last_verified_tree_slow").exists()   # slow-only stamp


def test_run_verify_slow_no_command_stamps_tree_as_cleared(tmp_path: Path) -> None:
    """An opt-out project (no slow/fast command) runs no gate but still stamps
    HEAD's tree green + verified, so the pre-merge clearance backstop treats an
    unconfigured project as cleared rather than deadlocking every merge."""
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    change_dir = project / ".imp" / "change"
    proc = _run("run-verify.sh", "slow",
                env_extra={"IMP_PROJECT_ROOT": str(project),
                           "IMP_CHANGE_DIR": str(change_dir)},
                env_remove=("IMP_TEST_CMD_FAST", "IMP_TEST_CMD_SLOW"))
    assert proc.returncode == 0, proc.stderr
    assert "no test command configured" in proc.stdout
    tree = _head_tree(project)
    assert (change_dir / "last_green_tree_slow").read_text().strip() == tree
    assert (change_dir / "last_verified_tree_slow").read_text().strip() == tree


def test_run_verify_fast_no_command_stamps_only_its_tier(tmp_path: Path) -> None:
    """A no-op FAST gate stamps only its own tier's green marker, never the slow
    clearance stamps."""
    project = tmp_path / "proj"
    project.mkdir()
    _git_init(project)
    (project / "a.txt").write_text("1")
    _git_commit_all(project, "c1")
    change_dir = project / ".imp" / "change"
    proc = _run("run-verify.sh", "fast",
                env_extra={"IMP_PROJECT_ROOT": str(project),
                           "IMP_CHANGE_DIR": str(change_dir)},
                env_remove=("IMP_TEST_CMD_FAST",))
    assert proc.returncode == 0, proc.stderr
    assert (change_dir / "last_green_tree_fast").read_text().strip() == _head_tree(project)
    assert not (change_dir / "last_green_tree_slow").exists()
    assert not (change_dir / "last_verified_tree_slow").exists()
