#!/usr/bin/env bash
# Establish the change's BASELINE verification state, ONCE, after the spec gate and
# before planning. A CLEAN baseline leaves no tolerated-failures record. A RED
# baseline captures the failure output into .imp/change/tolerated-failures.md tagged
# `baseline`, and exits non-zero so the workflow proceeds (continue: true); it NEVER pauses
# (re-running the suite later is the regular verify gate's job — here we only record
# the starting state). Locates .imp/change/ by fixed relative path after cd-ing to
# IMP_PROJECT_ROOT; there is no $IMP_CHANGE_DIR. No -e: we capture the verify exit
# code rather than dying on it.
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

if [[ -z "${IMP_VERIFY_COMMAND:-}" ]]; then
  echo "no verify_command configured; baseline is vacuously clean"
  exit 0
fi

verify_timeout="${IMP_VERIFY_TIMEOUT:-600}"
out=$(timeout -k 30 "$verify_timeout" bash -c "$IMP_VERIFY_COMMAND" 2>&1 | tee /dev/stderr)
rc=$?
if (( rc == 0 )); then
  echo "baseline verification clean"
  exit 0
fi

mkdir -p .imp/change
rec=".imp/change/tolerated-failures.md"
{
  echo "# Tolerated failures"
  echo
  echo "Failures imp will not block the merge on. Each entry is tagged with its"
  echo "origin (baseline | flaky) and a one-line justification; finalize surfaces"
  echo "every entry to a human before merge."
  echo
  echo "- [baseline] verification was already failing at the change's baseline"
  echo "  (before any task ran); these failures pre-date this change. Captured output:"
  echo
  echo '```'
  echo "$out"
  echo '```'
} > "$rec"
echo "baseline verification RED (exit $rc) — captured to $rec; proceeding (baseline never pauses)"
exit "$rc"
