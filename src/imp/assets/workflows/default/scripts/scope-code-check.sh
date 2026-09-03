#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# scope-code-check.sh — phase-boundary gate for the scoping (threshold) step.
# Scoping must produce ONLY a spec, never committed source; the pipeline builds
# from the spec. A well-behaved hand-off leaves HEAD == cur_base_head, so any
# commit beyond the fork point is illegitimate. Clean -> exit 0; committed code
# -> print recovery to stderr and exit 1 (the engine feeds stdout+stderr back to
# the resumed scoping turn as its feedback).
#
#   0 -> clean: no commits on the quest branch beyond the fork point
#   1 -> the scoping step committed code (or cur_base_head is missing / invalid)
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"

FORK=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['cur_base_head'])" "$QUEST_DIR/quest.json")
[ -n "$FORK" ] || { echo "empty fork (no cur_base_head in quest.json)" >&2; exit 1; }

# Fail closed: a git error (e.g. an invalid $FORK) must not read as "clean". Capture
# rev-list's own exit status rather than testing only its (empty-on-error) stdout.
if ! COMMITS=$(git rev-list "$FORK"..HEAD 2>/dev/null); then
  echo "git rev-list failed for range $FORK..HEAD — is cur_base_head a valid commit?" >&2
  exit 1
fi
if [ -z "$COMMITS" ]; then
  echo "ok: no commits on the quest branch beyond $FORK"
  exit 0
fi

{
  echo "The scoping phase committed code to the quest branch, which it must never do —"
  echo "scoping only produces a spec; the pipeline builds from it."
  echo "Offending commits ($FORK..HEAD):"
  git log --oneline "$FORK"..HEAD
  echo "Recover: run 'git reset --hard $FORK' to drop these commits (your spec lives"
  echo "outside the code tree and is unaffected), confirm 'git status' is clean, then"
  echo "finish the step again."
} >&2
exit 1
