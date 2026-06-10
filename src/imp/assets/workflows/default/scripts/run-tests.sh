#!/usr/bin/env bash
# Run the project's test suite using the wired IMP_TEST_COMMAND.
#
# Contract (mirrors the lint gate):
#   - IMP_TEST_COMMAND set and non-empty → run it via `bash -c` with a timeout
#     (IMP_TEST_TIMEOUT seconds, default 300); propagate its exit code. On
#     timeout (exit 124) print an explicit diagnosis to stderr — the truncated
#     output otherwise reads as a hang at whatever test happened to be printing,
#     and sends the resumed task-lead hunting a phantom hanging test.
#   - IMP_TEST_COMMAND unset or empty    → print an informational message and
#     exit 0 (no test gate; project hasn't wired a test_command).
#
# The wired test_command should be a fast, scoped suite — the gate runs on
# every implement loop, so 300 s is a budget, not just a wedge guard. A slower
# suite needs IMP_TEST_TIMEOUT raised (ambient env override; note the gate
# sandbox allowlists only engine-injected IMP_* vars, so under a sandbox
# policy the default applies).
#
# IMP_TEST_COMMAND is surfaced from the `test_command` project config key by
# overlay_engine_env and injected into script-gate env by the engine.
#
# Script gates run with cwd set to the Change's tracked workflow dir; cd into the
# project tree (IMP_PROJECT_ROOT, set by the engine) before running tests.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

if [[ -n "${IMP_TEST_COMMAND:-}" ]]; then
  test_timeout="${IMP_TEST_TIMEOUT:-300}"
  rc=0
  # -k 30: SIGKILL stragglers that ignore the TERM (pytest mid-teardown).
  timeout -k 30 "$test_timeout" bash -c "$IMP_TEST_COMMAND" || rc=$?
  if (( rc == 124 )); then
    echo "imp: test command timed out after ${test_timeout}s and was killed." >&2
    echo "imp: the output above is truncated mid-run — the suite did not fail" >&2
    echo "imp: at the last printed test; it ran out of time. Do not hunt for a" >&2
    echo "imp: hanging test. The suite is slower than the gate timeout." >&2
  fi
  exit "$rc"
fi

echo "no test_command configured; skipping test gate"
