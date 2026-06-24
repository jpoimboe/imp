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
# out of range, so autosquash cannot reorder it; it stays unfolded and the post-fold
# directive check trips. Cleanup BOUNCES the surviving directive back to the site's
# committing agent (exit 3) with a prescriptive diagnostic instead of pausing for a
# human; the agent fixes forward — converting the misfiled directive into a standalone
# commit or replacing it with a new forward commit. Cleanup never silently rewrites a
# commit at or below the lower bound, so the review pin stays stable and reviewed
# history is never clawed back.
# After 2 consecutive bounces with the directive still surviving, cleanup falls back
# to exit 1 so a human steps in; any clean run (exit 0) resets the bounce counter.
#
#   0 -> clean tree, fixups folded, trailers stamped (or already-clean no-op) -> continue
#   2 -> resolvable fold conflict (rebase left in progress) -> autosquash-resolve
#   3 -> dirty working tree, OR a surviving fixup directive bounced -> resume the site's agent
#   1 -> hard failure (rebase/stamp failure, empty bound), or surviving-directive bounce cap exhausted -> pause
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
STATE="$CHANGE_DIR/state"
MAX_BOUNCES=2

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

# Per surviving fixup!/squash!/amend! directive in range, emit a prescriptive line
# naming the offending commit, its target, and the forward-fix remedy. A target at or
# below LOWER is out of range (base or already-reviewed history); an unmatched target
# is a likely typo in the --fixup argument.
_diagnose_surviving_directives() {
  local lower_short sha subj short target_subj target_sha target_short
  lower_short=$(git rev-parse --short "$LOWER")
  echo "a fixup!/squash!/amend! directive survived autosquash and cannot be folded:" >&2
  while IFS=$'\t' read -r sha subj; do
    case "$subj" in
      fixup!*|squash!*|amend!*) ;;
      *) continue ;;
    esac
    short=$(git rev-parse --short "$sha")
    target_subj=${subj#*! }
    # No pipe partner: awk's `exit` on first match would SIGPIPE a `git log |` producer,
    # and under set -e + pipefail that non-zero pipeline aborts the script before `exit 3`.
    # Feed git log's full output via a here-string instead — the same SIGPIPE-avoidance
    # idiom _has_directives uses.
    target_sha=$(awk -F'\t' -v t="$target_subj" '$2==t{print $1; exit}' \
      <<< "$(git log --format='%H%x09%s')")
    if [ -n "$target_sha" ] && git merge-base --is-ancestor "$target_sha" "$LOWER"; then
      target_short=$(git rev-parse --short "$target_sha")
      echo "  - '$subj' ($short) targets '$target_subj' ($target_short), at or below the change fork $lower_short (base or already-reviewed history). Drop the 'fixup! ' prefix to land it as its own commit, or move the fix into a new forward commit." >&2
    else
      echo "  - '$subj' ($short) matches no commit in the change — likely a typo in the --fixup target. Reword it into a standalone commit, or replace it with a new forward commit." >&2
    fi
  done < <(git log --format='%H%x09%s' "$LOWER"..HEAD)
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
  rm -f "$STATE/directive-bounce-attempts"
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
      _diagnose_surviving_directives
      BOUNCE_FILE="$STATE/directive-bounce-attempts"
      PREV=$(cat "$BOUNCE_FILE" 2>/dev/null || echo 0)
      COUNT=$((PREV + 1))
      if [ "$COUNT" -gt "$MAX_BOUNCES" ]; then
        rm -f "$BOUNCE_FILE"
        echo "the committing agent could not clear the surviving directive after $MAX_BOUNCES bounces; pausing for a human." >&2
        exit 1
      fi
      mkdir -p "$STATE"
      printf '%s\n' "$COUNT" > "$BOUNCE_FILE"
      exit 3
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

rm -f "$STATE/directive-bounce-attempts"
echo "ok: clean tree, fixups folded, Change: $ID stamped over $LOWER..HEAD"
