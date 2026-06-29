#!/usr/bin/env bash
# Run the project's tiered verification gate. Takes one positional arg naming the
# tier to run: `fast` or `slow`.
#
#   fast → IMP_TEST_CMD_FAST under IMP_TEST_CMD_FAST_TIMEOUT (default 120s). The
#          cheap gate (lint + typecheck + fast tests); runs at the frequent
#          per-plan gates (verify, sweep-verify).
#   slow → IMP_TEST_CMD_SLOW under IMP_TEST_CMD_SLOW_TIMEOUT (default 300s). The
#          complete run (a superset of fast); runs once per Change at
#          integration-verify. When IMP_TEST_CMD_SLOW is empty it falls back to
#          IMP_TEST_CMD_FAST under the slow timeout.
#
# Contract:
#   - resolved command non-empty → run it via `bash -c` under a timeout;
#     propagate its exit code. On timeout (exit 124) print an explicit diagnosis
#     to stderr naming THIS tier's *_timeout config key — truncated output
#     otherwise reads as a hang at whatever check happened to be printing. The
#     workflow routes exit 124 to a human pause (re-running can't beat the clock),
#     distinct from a real non-zero failure which bounces back to the upstream
#     agent.
#   - resolved command unset or empty → print an informational message and exit 0
#     (no gate; project hasn't wired / has opted out of this tier).
#
# Per-tier tree-SHA skip cache: record the git tree SHA of the last green (exit-0)
# run in a per-tier per-change file (last_green_tree_<tier>) under $IMP_CHANGE_DIR.
# On entry, if HEAD's tree equals THIS tier's last green tree, skip (exit 0) —
# identical tree ⇒ identical result. Each tier has its own cache: a green fast run
# never lets the slow gate skip (and vice versa). Logged explicitly, never silent.
#
# IMP_TEST_CMD_* are surfaced from the test_cmd_fast / test_cmd_slow /
# test_cmd_fast_timeout / test_cmd_slow_timeout project config keys by
# overlay_engine_env and injected into script-gate env by the engine (all
# IMP_-prefixed, so the sandbox allowlist passes them through unchanged).
#
# Script gates run with cwd set to the Change's tracked workflow dir; cd into the
# project tree (IMP_PROJECT_ROOT, set by the engine) first.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

tier="${1:?usage: run-verify.sh <fast|slow>}"
case "$tier" in
  fast)
    cmd="${IMP_TEST_CMD_FAST:-}"
    gate_timeout="${IMP_TEST_CMD_FAST_TIMEOUT:-120}"
    timeout_key="test_cmd_fast_timeout"
    ;;
  slow)
    cmd="${IMP_TEST_CMD_SLOW:-}"
    if [[ -z "$cmd" ]]; then
      cmd="${IMP_TEST_CMD_FAST:-}"   # slow unset => run the fast suite at the slow ceiling
    fi
    gate_timeout="${IMP_TEST_CMD_SLOW_TIMEOUT:-300}"
    timeout_key="test_cmd_slow_timeout"
    ;;
  *)
    echo "run-verify.sh: unknown tier '$tier' (expected 'fast' or 'slow')" >&2
    exit 2
    ;;
esac

if [[ -z "$cmd" ]]; then
  echo "no test command configured; skipping $tier gate"
  exit 0
fi

cache="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}/last_green_tree_${tier}"
tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null || true)"
if [[ -n "$tree" && -f "$cache" && "$(cat "$cache")" == "$tree" ]]; then
  echo "$tier gate skipped — tree unchanged since last green run"
  exit 0
fi

rc=0
# -k 30: SIGKILL stragglers that ignore the TERM.
timeout -k 30 "$gate_timeout" bash -c "$cmd" || rc=$?
if (( rc == 124 )); then
  echo "imp: $tier test command timed out after ${gate_timeout}s and was killed." >&2
  echo "imp: the output above is truncated mid-run — verification did not fail" >&2
  echo "imp: at the last printed line; it ran out of time. Do not hunt for a" >&2
  echo "imp: hanging test. The command is slower than the gate timeout." >&2
  echo "imp: raise the ceiling with the '$timeout_key' config key (seconds)." >&2
fi

if (( rc == 0 )) && [[ -n "$tree" ]]; then
  mkdir -p "$IMP_CHANGE_DIR" && echo "$tree" > "$cache" || true
fi
exit "$rc"
