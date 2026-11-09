#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# Post-rebase environment re-sync: re-run the project's worktree_setup
# (IMP_WORKTREE_SETUP, surfaced by the engine) so the worktree's gitignored
# toolchain state (.venv, node_modules, ...) matches the rebased dependency
# manifests before the at-head gates. Writes NOTHING to stdout: the command's
# stdout is folded into stderr because the pause screen lists stderr before
# stdout, so the closing failure message stays last.
#   0  -> setup succeeded, or none configured -> validate-memory
#   N  -> setup failed (its exit code) or timed out (124) -> pause
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || { echo "could not enter project root; pausing" >&2; exit 5; }
if [ -z "${IMP_WORKTREE_SETUP:-}" ]; then
  echo "imp: no worktree_setup configured; skipping" >&2
  exit 0
fi
BASE=$("$IMP_PYTHON" -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_QUEST_DIR'],'quest.json')))['base_branch'])") || { echo "could not read base_branch from quest.json; pausing" >&2; exit 5; }
[ -n "$BASE" ] || { echo "base_branch is empty in quest.json; pausing" >&2; exit 5; }

rc=0
# Matches WORKTREE_SETUP_TIMEOUT_S, the go-live setup ceiling.
SETUP_TIMEOUT_S=600
timeout -k 30 "$SETUP_TIMEOUT_S" bash -c "$IMP_WORKTREE_SETUP" 1>&2 || rc=$?
[ "$rc" -eq 0 ] && exit 0
if [ "$rc" -eq 124 ]; then
  echo "imp: worktree_setup timed out after ${SETUP_TIMEOUT_S}s after rebasing onto $BASE: $IMP_WORKTREE_SETUP" >&2
else
  echo "imp: worktree_setup failed (exit $rc) after rebasing onto $BASE: $IMP_WORKTREE_SETUP" >&2
fi
exit "$rc"
