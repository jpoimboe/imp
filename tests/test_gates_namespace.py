"""Gate commands at single-command namespace scope (spec §4, AC 16).

Each test runs a REAL gate through gates.run_command under a real active policy, so
run_command opens a keeper, joins it and closes it around the one command. Roots live
under the default temp dir (/tmp), which build_sandbox_spawn binds by identity, so a
planted process is visible both inside the namespace and host-side.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid

import pytest

from imp.engine.errors import summarize_exception
from imp.engine.gates import GATE_KILL_GRACE_S, run_command
from imp.engine.sandbox import sandbox_available
from tests.test_sandbox_namespace import (
    _ALIVE,
    _host_pids,
    _make_root,
    _policy_for,
    _poll,
)

# The module timeout is the SINGLE outer backstop for a genuine hang; it exceeds the
# sum of the inner budgets any one test can spend, so the two stay independent.
pytestmark = [
    pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable"),
    pytest.mark.timeout(180),
]

_GATE_TIMEOUT_S = 60      # generous vs a sub-second gate; only a hang can reach it


def test_a_gate_command_leaves_no_stray_behind():
    # AC 16a: the gate detaches a process that breaks every group link imp has
    # (nohup + setsid), exits 0, and is still gone host-side once run_command
    # returns — the close is the reap, not the group kill.
    root = _make_root()
    marker = f"imp-gate-{uuid.uuid4().hex}"
    try:
        out = run_command(
            command=(f"nohup setsid sh -c '{_ALIVE}' {marker} >/dev/null 2>&1 &\n"
                     "echo started"),
            cwd=str(root), timeout_seconds=_GATE_TIMEOUT_S,
            policy=_policy_for(root))
        assert out["exit"] == 0
        assert out["stdout"] == "started"
        _poll(lambda: not _host_pids(marker),
              what=f"no host process carrying {marker} after run_command returned")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_gate_timeout_kills_the_group_before_closing_the_keeper():
    # AC 16b: the SIGTERM -> grace -> SIGKILL group kill still runs FIRST (a
    # SIGTERM-ignoring gate cannot finish before timeout + the grace window), and the
    # close then reaps the setsid child that escaped the group.
    root = _make_root()
    marker = f"imp-gate-{uuid.uuid4().hex}"
    command = (f"nohup setsid sh -c '{_ALIVE}' {marker} >/dev/null 2>&1 &\n"
               f"trap '' TERM\n{_ALIVE}\n")
    try:
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as ei:
            run_command(command=command, cwd=str(root), timeout_seconds=1,
                        policy=_policy_for(root))
        assert time.monotonic() - t0 >= 1 + GATE_KILL_GRACE_S
        message, _detail = summarize_exception(ei.value)
        # summarize_exception renders only str(e)'s FIRST line (errors.py:152), so the
        # marker has to stay on the command's first line for the `message` assertion;
        # the str(ei.value) one holds wherever it sits.
        assert marker in message            # names the gate command, not an nsenter argv
        assert marker in str(ei.value)
        assert "IMP_COMMAND" not in message
        _poll(lambda: not _host_pids(marker),
              what=f"{marker} gone once the keeper was closed")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_gate_commands_own_signal_reports_128_plus_n():
    # AC 16d: nsenter re-raises its child's signal on itself, so without the read-site
    # normalization this reports -9 and reads as imp having killed the gate.
    root = _make_root()
    try:
        out = run_command(command="kill -9 $$", cwd=str(root),
                          timeout_seconds=_GATE_TIMEOUT_S,
                          policy=_policy_for(root))
        assert out["exit"] == 137
    finally:
        shutil.rmtree(root, ignore_errors=True)
