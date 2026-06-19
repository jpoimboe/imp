#!/usr/bin/env bash
# At-head integration: the change is head of its merge-queue lane, so the base
# cannot move under it. Rebase the branch ONTO the base tip ONLY when the base
# moved (non-empty base-side incoming diff); otherwise merge directly. Linear
# history, no merge commit; the eventual branch->base merge is the only merge.
#   3  -> bad/missing base ref -> pause
#   5  -> dirty entry tree / leftover rebase / git refused to start -> pause
#   2  -> base unmoved (empty base-side incoming diff): merge directly, NO review
#   0  -> clean rebase (base moved, no conflict) -> integration-reviewer
#   6  -> rebase stopped on a conflict (left in progress) -> integrator
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 5
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
BASE=$(python3 -c "import json;print(json.load(open('$CHANGE_DIR/change.json'))['base_branch'])") || exit 5
[ -n "$BASE" ] || exit 5

# A bad base_branch makes `--is-ancestor` exit 128; verify the ref up front.
git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1 || exit 3

# Leftover in-progress rebase from a prior interrupted run -> pause (a human inspects).
if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
   || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "a rebase is already in progress at integration entry; pausing" >&2
  exit 5
fi

# Refuse a TRACKED-dirty entry tree so pre-existing changes aren't folded into a
# replayed commit. Scope to tracked only; untracked scratch is harmless.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "change worktree has uncommitted TRACKED changes at integration entry; pausing" >&2
  exit 5
fi

MB=$(git merge-base "$BASE" HEAD) || exit 5

# Base-side incoming diff: what base added since the fork point. Empty => base
# has not moved relative to this branch -> merge directly, no integration/review.
if git diff --quiet "$MB" "$BASE"; then
  rm -f "$CHANGE_DIR/integration-notes.md"
  echo "base ($BASE) unmoved since fork; merging directly"
  exit 2
fi

# Base moved: rebase the branch onto it. rerere replays any recorded resolution;
# --empty=drop drops a change commit base already contains.
rm -f "$CHANGE_DIR/integration-notes.md"
if git -c rerere.enabled=true -c rerere.autoupdate=true rebase --empty=drop "$BASE"; then
  echo "clean rebase onto $BASE (no conflicts); handing to integration-reviewer"
  exit 0
fi
if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
   || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "rebase stopped on a conflict (left in progress); handing to integrator" >&2
  exit 6
fi
echo "git refused to start the rebase (no rebase in progress); pausing for a human" >&2
exit 5
