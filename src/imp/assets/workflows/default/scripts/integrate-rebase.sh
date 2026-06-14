#!/usr/bin/env bash
# Rebase the change branch ONTO the base tip, inside the worktree, before review,
# so the branch's pre-merge history stays LINEAR and the eventual branch->base
# merge is the only merge commit. Classifies via exit code; the engine routes on it.
#
#   3  -> bad/missing base ref: fail loudly, never a silent empty integration
#   5  -> dirty entry tree / leftover in-progress rebase / git refused to start the
#         rebase -> pause; never hand the integrator a no-op "integration"
#   4  -> re-integration limit reached / bad IMP_MAX_REINTEGRATIONS -> pause
#   2  -> base already an ancestor of HEAD: nothing to integrate -> finalize
#   0  -> clean rebase completed (no conflict) -> integrate-verify, NO integrator
#   6  -> rebase stopped on a conflict (left in progress) -> integrator
set -uo pipefail
# Without `set -e`, guard the preamble explicitly: a failing cd (worktree race) or
# an unreadable change.json must not continue with empty context.
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 5
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])") || exit 5
[ -n "$BASE" ] || exit 5

# A bad base_branch makes `--is-ancestor` exit 128 (not 1), which would be misread
# as "not ancestor"; verify the ref up front instead.
git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1 || exit 3

# A leftover in-progress rebase from a prior interrupted run -> pause (the rebase
# analog of the stale-MERGE_HEAD guard; do NOT auto-abort, a human inspects).
if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
   || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "a rebase is already in progress at integration entry; pausing" >&2
  exit 5
fi

# Refuse a TRACKED-dirty entry tree so pre-existing tracked changes aren't folded
# into a replayed commit. Scope to tracked only; untracked scratch is harmless.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "change worktree has uncommitted TRACKED changes at integration entry; pausing" >&2
  exit 5
fi

CHANGE_DIR=".imp/change"

# Base already an ancestor of HEAD -> nothing to integrate. Record the SHA (base IS
# integrated) so base-fresh-check has a ref; do NOT bump the counter (no round).
if git merge-base --is-ancestor "$BASE" HEAD; then
  SHA=$(git rev-parse "${BASE}^{commit}") || exit 5
  echo "$SHA" > "$CHANGE_DIR/integrated_base_sha"
  rm -f "$CHANGE_DIR/integration-notes.md"
  echo "base ($BASE) already integrated; nothing to do"
  exit 2
fi

# Dedicated, persisted re-integration counter with CHECK-BEFORE-BUMP: at the limit,
# pause WITHOUT bumping (else every `imp resume` re-increments). Below --is-ancestor
# so only rounds that actually integrate count.
PREV=$(cat "$CHANGE_DIR/reintegration_count" 2>/dev/null || echo 0)
LIMIT="${IMP_MAX_REINTEGRATIONS:-10}"
if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then
  echo "IMP_MAX_REINTEGRATIONS must be a non-negative integer; got: '$LIMIT'" >&2
  exit 4
fi
if [ "$PREV" -ge "$LIMIT" ]; then
  echo "re-integration limit ($LIMIT) reached after $PREV rounds." >&2
  echo "To continue: set IMP_MAX_REINTEGRATIONS above $PREV, or reset .imp/change/reintegration_count, then \`imp resume\`." >&2
  exit 4
fi
echo "$((PREV + 1))" > "$CHANGE_DIR/reintegration_count"

# Record the base tip we are integrating BEFORE the rebase moves HEAD — that is the
# "we actually integrated base X" signal base-fresh-check checks against.
SHA=$(git rev-parse "${BASE}^{commit}") || exit 5
echo "$SHA" > "$CHANGE_DIR/integrated_base_sha"

# Rebase the branch onto base. rerere on every invocation (a conflict recurring
# across re-integration rounds replays its recorded resolution); --empty=drop (a
# change commit base already contains is dropped, not a stop on an empty pick).
rm -f "$CHANGE_DIR/integration-notes.md"
if git -c rerere.enabled=true -c rerere.autoupdate=true rebase --empty=drop "$BASE"; then
  echo "clean rebase onto $BASE (no conflicts); handing to integrate-verify"
  exit 0
fi
# Non-zero: either a conflict left the rebase in progress, or git refused to start.
if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
   || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "rebase stopped on a conflict (left in progress); handing to integrator" >&2
  exit 6
fi
echo "git refused to start the rebase (no rebase in progress); pausing for a human" >&2
exit 5
