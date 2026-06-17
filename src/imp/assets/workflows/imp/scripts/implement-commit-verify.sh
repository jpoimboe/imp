#!/usr/bin/env bash
# Validate the implement phase's commits. After the task-lead loop the ONLY
# structural check left is: the worktree is clean (the implementer committed
# everything it claimed). The `Change:` trailer is stamped later (stamp-trailers,
# review phase), so it cannot be checked here; per-task commit attribution was
# given up with the `Task:` apparatus, so there is no count check. Failure
# resumes task-lead with this message as feedback.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

# (1) clean worktree
if [[ -n "$(git status --porcelain)" ]]; then
  echo "uncommitted changes remain — the implementer must commit all task work:" >&2
  git status --porcelain >&2
  exit 1
fi

echo "ok: clean tree"
