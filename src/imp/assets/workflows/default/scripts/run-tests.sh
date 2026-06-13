#!/usr/bin/env bash
# Run the project's verify gate using the wired IMP_VERIFY_COMMAND.
#
# Contract:
#   - IMP_VERIFY_COMMAND set and non-empty → run it via `bash -c` with a timeout
#     (IMP_VERIFY_TIMEOUT seconds, default 300); propagate its exit code. On
#     timeout (exit 124) print an explicit diagnosis to stderr — the truncated
#     output otherwise reads as a hang at whatever step happened to be printing,
#     and sends the resumed task-lead hunting a phantom hanging command.
#   - IMP_VERIFY_COMMAND unset or empty   → print an informational message and
#     exit 0 (no verify gate; project hasn't wired a verify_command).
#
# The wired verify_command should be a fast, scoped suite — the gate runs on
# every implement loop, so 300 s is a budget, not just a wedge guard. A slower
# suite needs IMP_VERIFY_TIMEOUT raised (ambient env override; note the gate
# sandbox allowlists only engine-injected IMP_* vars, so under a sandbox
# policy the default applies).
#
# IMP_VERIFY_COMMAND is surfaced from the `verify_command` project config key by
# overlay_engine_env and injected into script-gate env by the engine.
#
# Script gates run with cwd set to the Change's tracked workflow dir; cd into the
# project tree (IMP_PROJECT_ROOT, set by the engine) before running the verify command.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

if [[ -n "${IMP_VERIFY_COMMAND:-}" ]]; then
  verify_timeout="${IMP_VERIFY_TIMEOUT:-300}"
  rc=0
  # -k 30: SIGKILL stragglers that ignore the TERM (pytest mid-teardown).
  timeout -k 30 "$verify_timeout" bash -c "$IMP_VERIFY_COMMAND" || rc=$?
  if (( rc == 124 )); then
    echo "imp: verify command timed out after ${verify_timeout}s and was killed." >&2
    echo "imp: the output above is truncated mid-run — the suite did not fail" >&2
    echo "imp: at the last printed step; it ran out of time. Do not hunt for a" >&2
    echo "imp: hanging command. The suite is slower than the gate timeout." >&2
  fi
  exit "$rc"
fi

echo "no verify_command configured; skipping verify gate"
