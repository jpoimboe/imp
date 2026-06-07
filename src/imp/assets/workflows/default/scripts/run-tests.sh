#!/usr/bin/env bash
# Run the project's test suite using the wired IMP_TEST_COMMAND.
#
# Contract (mirrors the lint gate):
#   - IMP_TEST_COMMAND set and non-empty → run it via `bash -c` with a 300 s
#     timeout; propagate its exit code.
#   - IMP_TEST_COMMAND unset or empty    → print an informational message and
#     exit 0 (no test gate; project hasn't wired a test_command).
#
# IMP_TEST_COMMAND is surfaced from the `test_command` project config key by
# overlay_engine_env and injected into script-gate env by the engine.
#
# Script gates run with cwd set to the workflow snapshot dir; cd into the
# project tree (IMP_PROJECT_ROOT, set by the engine) before running tests.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

if [[ -n "${IMP_TEST_COMMAND:-}" ]]; then
  exec timeout 300 bash -c "$IMP_TEST_COMMAND"
fi

echo "no test_command configured; skipping test gate"
