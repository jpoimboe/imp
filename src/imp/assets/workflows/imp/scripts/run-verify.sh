#!/usr/bin/env bash
# Run the project's single combined verification command (IMP_VERIFY_COMMAND),
# replacing the old test/lint gates. Tests, lint, and typecheck are whatever the
# project chains into that one command; imp runs it and routes back on a non-zero
# exit.
#
# Contract:
#   - IMP_VERIFY_COMMAND set and non-empty → run it via `bash -c` under a timeout
#     (IMP_VERIFY_TIMEOUT seconds, default 600); propagate its exit code. On
#     timeout (exit 124) print an explicit diagnosis to stderr — truncated output
#     otherwise reads as a hang at whatever check happened to be printing. The
#     workflow routes exit 124 to a human pause (re-running can't beat the clock),
#     distinct from a real non-zero failure which bounces back to the implementer.
#   - IMP_VERIFY_COMMAND unset or empty → print an informational message and exit 0
#     (no verify gate; project hasn't wired / has opted out of verify_command).
#
# Tree-SHA skip cache: record the git tree SHA of the last green (exit-0) run in a
# per-change file under .imp/change/. On entry, if HEAD's tree equals the last
# green tree, skip (exit 0) — identical tree ⇒ identical result. Logged explicitly,
# never silent.
#
# IMP_VERIFY_COMMAND / IMP_VERIFY_TIMEOUT are surfaced from the `verify_command` /
# `verify_timeout` project config keys by overlay_engine_env and injected into
# script-gate env by the engine (both IMP_-prefixed, so the sandbox allowlist
# passes them through unchanged).
#
# Script gates run with cwd set to the Change's tracked workflow dir; cd into the
# project tree (IMP_PROJECT_ROOT, set by the engine) first.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

if [[ -z "${IMP_VERIFY_COMMAND:-}" ]]; then
  echo "no verify_command configured; skipping verify gate"
  exit 0
fi

cache=".imp/change/last_green_tree"
tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null || true)"
if [[ -n "$tree" && -f "$cache" && "$(cat "$cache")" == "$tree" ]]; then
  echo "verify skipped — tree unchanged since last green run"
  exit 0
fi

verify_timeout="${IMP_VERIFY_TIMEOUT:-600}"
rc=0
# -k 30: SIGKILL stragglers that ignore the TERM.
timeout -k 30 "$verify_timeout" bash -c "$IMP_VERIFY_COMMAND" || rc=$?
if (( rc == 124 )); then
  echo "imp: verify command timed out after ${verify_timeout}s and was killed." >&2
  echo "imp: the output above is truncated mid-run — verification did not fail" >&2
  echo "imp: at the last printed line; it ran out of time. Do not hunt for a" >&2
  echo "imp: hanging test. The command is slower than the gate timeout." >&2
  echo "imp: raise the ceiling with the 'verify_timeout' config key (seconds)." >&2
fi

if (( rc == 0 )) && [[ -n "$tree" ]]; then
  mkdir -p .imp/change && echo "$tree" > "$cache" || true
fi
exit "$rc"
