#!/usr/bin/env bash
# cleanup.sh — the ONE end-of-change fold + stamp, run once at cleanup-merge over
# the whole change range cur_base_head..HEAD. It folds every fixup!/squash!/amend!
# chain into its target and stamps `Change: <id>` on every non-merge commit. Both
# are idempotent: fold runs only when a directive exists, stamp only when some
# in-range commit lacks the trailer, so re-running on already-folded, already-
# stamped history is a ZERO-CHURN no-op (identical SHAs). Because the whole change
# is in range, a fixup! targeting ANY change commit folds.
#
#   0 -> clean tree, fixups folded, trailers stamped (or already-clean no-op)
#   2 -> resolvable fold conflict (rebase left in progress) -> autosquash-resolve-merge
#   1 -> pause for a human: dirty working tree, a surviving fixup directive that
#        cannot be folded, or a hard rebase/stamp failure
set -uo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
ID="${IMP_QUEST_ID:?IMP_QUEST_ID not set by engine}"
STATE="$QUEST_DIR/state"

# Range base: the change fork (cur_base_head from quest.json). The whole change is
# in range, so any in-range fixup! folds.
FORK=$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_QUEST_DIR'],'quest.json')))['cur_base_head'])")
[ -n "$FORK" ] || { echo "empty fork (no cur_base_head)" >&2; exit 1; }

# grep -q in a pipeline with pipefail is unreliable (SIGPIPE makes git log fail even
# on a match); read subjects via a here-string with no pipe partner.
_has_directives() {
  grep -qE '^(fixup|squash|amend)!' <<< "$(git log --format='%s' "$FORK"..HEAD)"
}

# Every NON-MERGE commit in range already carries the trailer.
_all_stamped() {
  local h
  while read -r h; do
    [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$ID" ] \
      || return 1
  done < <(git rev-list --no-merges "$FORK"..HEAD)
  return 0
}

# Per surviving fixup!/squash!/amend! directive in range, emit a prescriptive line
# naming the offending commit and the forward-fix remedy. A directive survives
# autosquash only when its target matches no in-range commit — a typo in the
# --fixup argument, or a target dropped during integration (integrate-rebase
# --empty=drop).
_diagnose_surviving_directives() {
  local sha subj short
  echo "a fixup!/squash!/amend! directive survived autosquash and cannot be folded:" >&2
  while IFS=$'\t' read -r sha subj; do
    case "$subj" in
      fixup!*|squash!*|amend!*) ;;
      *) continue ;;
    esac
    short=$(git rev-parse --short "$sha" 2>/dev/null) || short="$sha"
    echo "  - '$subj' ($short) matches no commit in $FORK..HEAD — its target is a typo in the --fixup argument, or a commit dropped during integration. Reword it into a standalone commit, or replace it with a new forward commit." >&2
  done < <(git log --format='%H%x09%s' "$FORK"..HEAD)
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
    REASON="a fixup!/squash!/amend! subject still remains in $FORK..HEAD"
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
      echo "  git rebase -i --autosquash $FORK" >&2
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
# Defensive backstop: the engine clean_tree invariant runs on the preceding agent
# steps, so a dirty tree here is near-unreachable. Pause for a human if it happens.
if [ -n "$(git status --porcelain)" ]; then
  echo "uncommitted changes remain — commit all work before cleanup:" >&2
  git status --porcelain >&2
  exit 1
fi

# Fast path: nothing to fold AND every commit already stamped -> a true no-op
# (no rebase, no SHA churn). An empty range stamps vacuously and lands here too.
if ! _has_directives && _all_stamped; then
  echo "ok: clean tree, no fixups, already stamped over $FORK..HEAD"
  exit 0
fi

# ── FOLD MODE ────────────────────────────────────────────────────────────────
# The deterministic fold runs under `set -e` (it relies on -e to abort on an
# unexpected git failure mid-rebase).
set -e
if _has_directives; then
  BEFORE=$(git rev-parse HEAD)
  BEFORE_TREE=$(git rev-parse "HEAD^{tree}")
  if GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$FORK"; then
    if _has_directives; then
      _diagnose_surviving_directives
      echo "the surviving directive cannot be folded; pausing for a human." >&2
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
# trailer.ifexists=doNothing. Then self-verify the postcondition. (Reached only
# when a directive was folded or some commit was unstamped; the fast path above
# already returned for already-folded + already-stamped history, keeping re-entry
# zero-churn.)
if ! git rebase "$FORK" \
  --exec 'git -c trailer.ifexists=doNothing commit --amend --no-edit --trailer "Change: '"$ID"'"'; then
  git rebase --abort 2>/dev/null || true
  echo "cleanup stamp rebase failed; branch left intact for human resolution" >&2
  exit 1
fi

while read -r h; do
  [ "$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h")" = "$ID" ] \
    || { echo "stamp failed: $h missing Change: $ID" >&2; exit 1; }
done < <(git rev-list --no-merges "$FORK"..HEAD)

echo "ok: clean tree, fixups folded, Change: $ID stamped over $FORK..HEAD"
