#!/usr/bin/env bash
# Integrate the base branch INTO the change branch, inside the worktree, before
# review — so the eventual branch->base merge is trivial. Leaves a clean-staged
# OR conflicted, UNCOMMITTED tree for the integrator agent to finish. Classifies
# via exit code; the engine routes on it.
#
#   3  -> bad/missing base ref (R7): fail loudly, never a silent empty integration
#   5  -> dirty entry tree, or git REFUSED to start the merge (no MERGE_HEAD) ->
#         pause; we must never hand the integrator a no-op "integration"
#   2  -> branch already contains the base tip: nothing to integrate
#   0  -> a merge is in progress (clean-staged or conflicted) for the integrator
set -uo pipefail
# Without `set -e`, guard the preamble explicitly: a failing cd (worktree race) or
# an unreadable change.json must not continue with empty context and misdiagnose
# downstream as "bad base ref".
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 5
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])") || exit 5
[ -n "$BASE" ] || exit 5

# R7: a bad base_branch makes `--is-ancestor` exit 128 (not 1), which the `if`
# below would read as "not ancestor", then the merge fails but is swallowed —
# handing the integrator a tree with no merge in progress and no incoming diff,
# indistinguishable from a trivial clean merge. Verify the ref up front instead.
git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1 || exit 3

# A merge already in progress from a prior interrupted run (MERGE_HEAD present)
# would make the `git merge` below refuse, and the post-merge MERGE_HEAD check
# would then see this STALE MERGE_HEAD and falsely report exit 0. The integrator
# always concludes/commits its merge, so a MERGE_HEAD here means an abnormal
# leftover -> pause for a human (consistent with the exit-5 "never hand a no-op").
if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
  echo "a merge is already in progress (stale MERGE_HEAD) at integration entry; pausing" >&2
  exit 5
fi

# The integrator commits its resolution every round. Refuse a TRACKED-dirty tree
# on entry so pre-existing staged/unstaged tracked changes aren't silently folded
# into the merge commit. Scope to tracked only (`--untracked-files=no`, review
# finding #9): untracked scratch files are routine and harmless — `git commit` of
# the merge never folds them in, and the only untracked files that matter are ones
# the incoming merge would overwrite, which git itself refuses (the no-MERGE_HEAD
# branch below catches that). The relocated H1 net only fires AFTER the integrator
# commits, so this entry guard is what protects the pre-integrator window.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "change worktree has uncommitted TRACKED changes at integration entry; pausing" >&2
  exit 5
fi

CHANGE_DIR=".imp/change"

# Branch already contains the base tip -> nothing to integrate. Record the SHA
# here (base IS integrated, so the current tip is the correct recorded value) so
# base-fresh-check has a ref; do NOT bump the counter (no round happened).
if git merge-base --is-ancestor "$BASE" HEAD; then
  SHA=$(git rev-parse "${BASE}^{commit}") || exit 5
  echo "$SHA" > "$CHANGE_DIR/integrated_base_sha"
  echo "base ($BASE) already integrated; nothing to do"
  exit 2
fi

# R2: a dedicated, persisted re-integration counter — NOT per-step max_loops. The
# backward `goto integrate-merge` is issued from TWO steps (base-fresh-check and
# merge-to-base), so per-step loop counters would split the true round count and
# bound neither. Below --is-ancestor so only rounds that actually integrate count.
# CHECK-BEFORE-BUMP (finding #8): if already AT the limit, pause WITHOUT bumping —
# else every `imp resume` re-increments and raising the limit by one re-trips
# immediately. The count parks at the limit; raising IMP_MAX_REINTEGRATIONS above
# it (or resetting the file) lets exactly the intended number of rounds proceed.
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

# Start the merge, uncommitted (F6 classification on MERGE_HEAD). Record the SHA
# on either merge-started path (clean or conflicted) — that is the "we actually
# integrated base X" signal the merge phase checks against. A refusal records
# nothing and pauses.
if git merge --no-ff --no-commit "$BASE"; then
  SHA=$(git rev-parse "${BASE}^{commit}") || exit 5
  echo "$SHA" > "$CHANGE_DIR/integrated_base_sha"
  echo "clean auto-merge staged (uncommitted); handing to integrator"
  exit 0
fi
if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
  SHA=$(git rev-parse "${BASE}^{commit}") || exit 5
  echo "$SHA" > "$CHANGE_DIR/integrated_base_sha"
  echo "merge conflicts (uncommitted); handing to integrator to resolve"
  exit 0
fi
echo "git refused to start the merge (no MERGE_HEAD); pausing for a human" >&2
exit 5
