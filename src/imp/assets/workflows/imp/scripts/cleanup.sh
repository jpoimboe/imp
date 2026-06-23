#!/usr/bin/env bash
# cleanup.sh — the ONE clean-tree -> fold -> stamp ritual, run at every
# production->consumption boundary (after task-lead, after deferred-sweep, after
# reconciler). It enforces the invariant that no commit reaches a consumer
# (reviewer, stamp, merge) without a clean tree, folded fixups, and a stamped
# `Change: <id>` trailer, all over the SAME range (arg or fork)..HEAD.
#
# The lower bound is $1 (the workflow passes `{{ vars.review_base }}`); an empty
# argument falls back to `fork` (= change.json cur_base_head). Operating over
# (arg or fork)..HEAD for BOTH the fold and the stamp gives a structural invariant:
# cleanup never rewrites a commit at or below the lower bound, so the review pin is
# stable by construction and per-batch stamping is safe.
#
# DELIBERATE SEMANTICS: a `fixup!` whose target is at or below the lower bound is
# out of range, so autosquash cannot reorder it; it stays unfolded, the post-fold
# directive check trips, and cleanup exits 1 -> a human. A fixup clawing back into
# already-reviewed history deserves a human, not a silent rewrite of reviewed
# commits (which would orphan the review pin).
#
#   0 -> clean tree, fixups folded, trailers stamped (or already-clean no-op) -> continue
#   2 -> resolvable fold conflict (rebase left in progress) -> autosquash-resolve
#   3 -> dirty working tree (agent left uncommitted work) -> resume the site's agent
#   1 -> hard failure / out-of-range fixup target -> pause
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
STATE="$CHANGE_DIR/state"

# Lower bound: the review_base SHA passed as $1, else the recorded fork point.
LOWER="${1:-}"
if [ -z "$LOWER" ]; then
  LOWER=$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_CHANGE_DIR'],'change.json')))['cur_base_head'])")
fi
[ -n "$LOWER" ] || { echo "empty lower bound (no arg and no cur_base_head)" >&2; exit 1; }

# grep -q in a pipeline with pipefail is unreliable (SIGPIPE makes git log fail even
# on a match); read subjects via a here-string with no pipe partner.
_has_directives() {
  grep -qE '^(fixup|squash|amend)!' <<< "$(git log --format='%s' "$LOWER"..HEAD)"
}

# Every NON-MERGE commit in range already carries the trailer.
_all_stamped() {
  local h
  while read -r h; do
    [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$ID" ] \
      || return 1
  done < <(git rev-list --no-merges "$LOWER"..HEAD)
  return 0
}

# ── VERIFY MODE ──────────────────────────────────────────────────────────────
# A before-tree sentinel means a fold conflict was handed to autosquash-resolve,
# which has now run; this re-entry VERIFIES its resolution rather than re-folding.
# A fixup! fold is tree-preserving, so a correct resolution reproduces the
# pre-fold tree exactly: HEAD^{tree} == BEFORE_TREE, no leftover directive, no
# rebase in progress. This block owns its own 3-attempt bound and the terminal
# pristine restore (the engine's max_loops cannot restore the branch itself). On
# success it falls through to the stamp below over the same range.
if [ -f "$STATE/autosquash-before-tree" ]; then
  BEFORE_TREE=$(cat "$STATE/autosquash-before-tree" 2>/dev/null || true)
  BEFORE_HEAD=$(cat "$STATE/autosquash-before-head" 2>/dev/null || true)
  ATTEMPTS_FILE="$STATE/autosquash-resolve-attempts"
  MAX_ATTEMPTS=3

  _clear_sentinels() {
    rm -f "$STATE/autosquash-before-tree" "$STATE/autosquash-before-head" "$ATTEMPTS_FILE"
  }

  # First objective failure (empty REASON => verified). An in-progress rebase makes
  # the tree check meaningless, so report it first.
  REASON=""
  if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
     || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
    REASON="a rebase is still in progress (the fold was not driven to completion)"
  elif _has_directives; then
    REASON="a fixup!/squash!/amend! subject still remains in $LOWER..HEAD"
  elif [ "$(git rev-parse 'HEAD^{tree}')" != "$BEFORE_TREE" ]; then
    REASON="HEAD tree does not match the pre-fold tree (the fold changed content)"
  fi

  if [ -n "$REASON" ]; then
    PREV=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)
    COUNT=$((PREV + 1))
    echo "$COUNT" > "$ATTEMPTS_FILE"
    if [ "$COUNT" -ge "$MAX_ATTEMPTS" ]; then
      # Bound exhausted: restore the branch to its exact pre-fold commit ourselves,
      # then pause. Abort any in-progress rebase first.
      git rebase --abort 2>/dev/null || true
      if [ -n "$BEFORE_HEAD" ]; then git reset -q --hard "$BEFORE_HEAD" 2>/dev/null || true; fi
      _clear_sentinels
      echo "autosquash-resolve exhausted $MAX_ATTEMPTS attempts: $REASON" >&2
      echo "Restored the branch to its pre-fold state. Squash by hand, then resume:" >&2
      echo "  git rebase -i --autosquash $LOWER" >&2
      echo "resolve each conflict so the fold preserves the tree, then \`imp resume\`." >&2
      exit 1
    fi
    echo "cleanup: $REASON (attempt $COUNT/$MAX_ATTEMPTS); re-resolving" >&2
    exit 2
  fi

  # Verified: clear the sentinels and fall through to the clean-tree check + stamp.
  _clear_sentinels
fi

# ── CLEAN-TREE CHECK ─────────────────────────────────────────────────────────
# The agent must have committed everything; a dirty tree means work was left behind.
if [ -n "$(git status --porcelain)" ]; then
  echo "uncommitted changes remain — commit all work before cleanup:" >&2
  git status --porcelain >&2
  exit 3
fi

# Fast path: nothing to fold AND every commit already stamped -> a true no-op
# (no rebase, no SHA churn). An empty range stamps vacuously and lands here too.
if ! _has_directives && _all_stamped; then
  echo "ok: clean tree, no fixups, already stamped over $LOWER..HEAD"
  exit 0
fi

# ── FOLD MODE ────────────────────────────────────────────────────────────────
# The deterministic fold runs under `set -e` (it relies on -e to abort on an
# unexpected git failure mid-rebase).
set -e
if _has_directives; then
  BEFORE=$(git rev-parse HEAD)
  BEFORE_TREE=$(git rev-parse "HEAD^{tree}")
  if GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$LOWER"; then
    if _has_directives; then
      echo "fixup!/squash!/amend! subject remains after autosquash — target out of range (at or below $LOWER) or unmatched" >&2
      exit 1
    fi
  else
    # A genuine fold CONFLICT (rebase in progress AND unmerged paths) is agent-
    # resolvable: leave it in progress and hand off. Any other failure is hard.
    if [ -d "$(git rev-parse --git-path rebase-merge)" ] && [ -n "$(git ls-files -u)" ]; then
      mkdir -p "$STATE"
      rm -f "$STATE/autosquash-resolve-attempts"
      printf '%s\n' "$BEFORE_TREE" > "$STATE/autosquash-before-tree"
      printf '%s\n' "$BEFORE" > "$STATE/autosquash-before-head"
      echo "cleanup: fixup fold conflicted; handing to autosquash-resolve (rebase left in progress)" >&2
      exit 2
    fi
    git rebase --abort 2>/dev/null || true
    echo "cleanup rebase failed with no resolvable fold conflict; branch left intact for human resolution" >&2
    exit 1
  fi
fi

# ── STAMP ────────────────────────────────────────────────────────────────────
# Stamp Change: <id> on every non-merge commit in range; idempotent via
# trailer.ifexists=doNothing. Then self-verify the postcondition.
if ! git rebase "$LOWER" \
  --exec 'git -c trailer.ifexists=doNothing commit --amend --no-edit --trailer "Change: '"$ID"'"'; then
  git rebase --abort 2>/dev/null || true
  echo "cleanup stamp rebase failed; branch left intact for human resolution" >&2
  exit 1
fi

while read -r h; do
  [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$ID" ] \
    || { echo "stamp failed: $h missing Change: $ID" >&2; exit 1; }
done < <(git rev-list --no-merges "$LOWER"..HEAD)

echo "ok: clean tree, fixups folded, Change: $ID stamped over $LOWER..HEAD"
