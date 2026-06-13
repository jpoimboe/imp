#!/usr/bin/env bash
# Fold every `fixup!` commit into its target so the merged branch is one clean
# commit per unit (Resolution 6). Runs once at review-phase entry. No-op when
# there are no fixups. A plain-path fold conflict is handed to autosquash-resolve (exit 2); other
# failures abort and exit non-zero → the engine pauses for a human.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")
# Rebase onto the FORK POINT, not the base tip — so we only reorder/squash this
# change's own commits and never pull in base commits that landed during the change.
FORK=$(git merge-base "$BASE" HEAD)

# Fast path: nothing to fold. Match every directive the rest of the script folds
# (fixup!/squash!/amend!) — a branch reworked with only squash!/amend! still needs
# autosquash, and `git rebase --autosquash` reorders all three.
if ! git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
  echo "no fixup!/squash!/amend! commits to autosquash"
  exit 0
fi

# Print the manual recovery the human should run when imp won't auto-fold a
# pre-merge fixup (see docs/autosquash-after-integration-recovery.md).
print_manual_recovery() {
  echo "Squash it by hand, recreating the merge so its resolution is preserved:" >&2
  echo "  git rebase -i --rebase-merges --autosquash \"\$(git merge-base $BASE HEAD)\"" >&2
  echo "resolve any merge conflict the way the integration was resolved, then \`imp resume\`." >&2
  echo "See docs/autosquash-after-integration-recovery.md." >&2
}

# Fold a fixup whose target is AT/BELOW an integration merge. This requires
# recreating the merge (`git rebase --rebase-merges`), which re-runs the merge and
# would drop the integrator's manual resolution. We make it safe two ways:
#   1. Seed `git rerere` with each integration merge's recorded resolution, so the
#      recreate replays it. rerere only applies when the conflict's preimage matches
#      byte-for-byte, so a fixup that overlaps a resolved region simply leaves the
#      conflict unresolved — we detect that and refuse.
#   2. Verify the fold preserved the FINAL TREE. Folding fixups must never change
#      the tree at HEAD; any mismatch means a resolution was dropped or altered
#      (rerere couldn't carry it, or a semantic edit baked into a clean merge was
#      lost). On mismatch we reset and refuse. This is a total guard: we never ship
#      a recreated merge whose tree differs from the verified original.
# Returns 0 on a safe auto-fold; 1 (branch restored pristine) when a human must do it.
fold_preserving_merges() {
  local BRANCH RR_CACHE PRE_RR
  BRANCH=$(git symbolic-ref --short -q HEAD || true)
  RR_CACHE=$(git rev-parse --git-path rr-cache)
  # Snapshot pre-existing rerere entries so cleanup prunes only what we add — never
  # the user's own rerere database (the change worktree shares the repo's rr-cache).
  PRE_RR=$(find "$RR_CACHE" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | sort || true)
  local RR=(-c rerere.enabled=true -c rerere.autoupdate=true)

  _cleanup_rr() {
    local now; now=$(find "$RR_CACHE" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | sort || true)
    local d
    while IFS= read -r d; do
      [ -n "$d" ] && rm -rf "${RR_CACHE:?}/$d"
    done < <(comm -13 <(printf '%s\n' "$PRE_RR") <(printf '%s\n' "$now"))
  }
  _restore() {
    git rebase --abort 2>/dev/null || true
    git merge --abort 2>/dev/null || true
    git reset -q --hard "$BEFORE" 2>/dev/null || true
    if [ -n "$BRANCH" ]; then git checkout -q "$BRANCH" 2>/dev/null || true; fi
  }

  # 1) Train rerere on every (two-parent) integration merge in range.
  local m
  for m in $(git rev-list --merges "$FORK"..HEAD); do
    # Skip octopus merges (integration merges are always two-parent); rerere
    # training of an n-way merge is out of scope — it falls to the tree check.
    git rev-parse -q --verify "${m}^3" >/dev/null 2>&1 && continue
    git "${RR[@]}" checkout -q "${m}^1" 2>/dev/null || { _restore; _cleanup_rr; return 1; }
    if ! git "${RR[@]}" merge --no-ff --no-commit "${m}^2" >/dev/null 2>&1; then
      # Recreated the conflict; record the merge's resolved tree as the resolution.
      git "${RR[@]}" checkout -q "$m" -- . 2>/dev/null || true
      git "${RR[@]}" rerere >/dev/null 2>&1 || true
    fi
    git merge --abort 2>/dev/null || true
    git reset -q --hard "$BEFORE" 2>/dev/null || true
  done
  if [ -n "$BRANCH" ]; then git checkout -q "$BRANCH" 2>/dev/null || true; fi

  # 2) Rebase the whole range, recreating merges; rerere replays the resolutions.
  GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true \
    git "${RR[@]}" rebase -i --rebase-merges --autosquash "$FORK" >/dev/null 2>&1 || true

  # 3) Drive to completion. A recreated merge that rerere fully resolved still stops
  #    the rebase, so we --continue past it; but ANY unresolved path means the fixup
  #    overlapped a resolved region — not safe to auto-fold, so abort and refuse.
  local guard=0
  while [ -d "$(git rev-parse --git-path rebase-merge)" ] \
     || [ -d "$(git rev-parse --git-path rebase-apply)" ]; do
    guard=$((guard + 1)); [ "$guard" -gt 50 ] && break
    if git diff --name-only --diff-filter=U | grep -q .; then
      _restore; _cleanup_rr
      echo "autosquash: a fixup!/squash!/amend! targets a commit at/below the integration" >&2
      echo "merge, and recreating the merge to fold it hit a conflict the recorded" >&2
      echo "resolution can't cover (the fix overlaps a resolved region)." >&2
      echo "Refusing; branch left intact." >&2
      print_manual_recovery
      return 1
    fi
    GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true \
      git "${RR[@]}" rebase --continue >/dev/null 2>&1 || true
  done

  # 4) Safety gate: folding fixups must preserve the final tree, keep at least one
  #    merge, and leave no leftover directive. Any failure ⇒ a resolution was lost;
  #    reset to the verified state and refuse rather than ship a changed tree.
  local AFTER_TREE MERGES_AFTER
  AFTER_TREE=$(git rev-parse "HEAD^{tree}" 2>/dev/null || true)
  MERGES_AFTER=$(git rev-list --merges "$FORK"..HEAD 2>/dev/null | wc -l)
  if [ "$AFTER_TREE" != "$BEFORE_TREE" ] || [ "$MERGES_AFTER" -lt 1 ] \
     || git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
    _restore; _cleanup_rr
    echo "autosquash: recreating the integration merge to fold a pre-merge fixup did not" >&2
    echo "reproduce the resolved tree — a manual resolution would be dropped. Refusing;" >&2
    echo "branch left intact." >&2
    print_manual_recovery
    return 1
  fi

  _cleanup_rr
  echo "autosquash: folded a pre-merge fixup; integration merge(s) recreated, resolutions preserved"
  return 0
}

# A prior base integration leaves a real merge commit in FORK..HEAD. Plain
# `rebase -i --autosquash $FORK` would linearize it, silently dropping the
# integrator's in-merge conflict resolutions (review finding #3). When a merge is
# in range, first try the cheap, always-safe path: rebase only the segment ABOVE
# the most recent integration merge, so a fixup! that targets a POST-merge commit
# folds while the merge (and the resolutions baked into its tree) is left untouched.
#
# If that leaves a leftover, the fixup targets a commit AT/BELOW a merge; fall back
# to fold_preserving_merges, which recreates the merge under rerere + a tree-equality
# guard (or refuses, pristine, when the fold can't be proven safe).
MERGE=$(git rev-list --merges --max-count=1 "$FORK"..HEAD)
if [ -n "$MERGE" ]; then
  BEFORE=$(git rev-parse HEAD)
  BEFORE_TREE=$(git rev-parse "HEAD^{tree}")
  # GIT_EDITOR=true as well: a squash! fold opens the commit-MESSAGE editor
  # (fixup! does not), which wedges editor-less environments (sandboxed gates,
  # CI). `true` accepts the default concatenated message — correct for an
  # unattended workflow step. Same pin fold_preserving_merges already uses.
  if GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$MERGE"; then
    if ! git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
      echo "autosquash: folded post-merge fixups; integration merge preserved"
      exit 0
    fi
    # Leftover ⇒ a directive targets a commit at/below the merge. Reset the cheap
    # rebase and try the merge-preserving fold.
    git reset --hard "$BEFORE" >/dev/null
    if fold_preserving_merges; then exit 0; else exit 1; fi
  fi
  git rebase --abort 2>/dev/null || true
  echo "autosquash rebase conflicted; branch left intact for human resolution" >&2
  exit 1
fi

# No integration merge in range: ordinary autosquash from the fork point.
# Capture the pre-autosquash HEAD + tree BEFORE starting the rebase. A fixup! fold is
# tree-preserving by definition, so these are the oracle the later autosquash-verify
# step checks the agent's resolution against — and the HEAD it resets to on a pristine
# abort. Persist them only when we actually hand off a conflict (exit 2).
BEFORE=$(git rev-parse HEAD)
BEFORE_TREE=$(git rev-parse "HEAD^{tree}")
# GIT_SEQUENCE_EDITOR=true accepts the todo list --autosquash has already reordered
# (fixups placed under their targets); GIT_EDITOR=true accepts the concatenated
# message a squash! fold opens the editor for (else editor-less envs wedge).
if GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true git rebase -i --autosquash "$FORK"; then
  if git log --format='%s' "$FORK"..HEAD | grep -qE '^(fixup|squash|amend)!'; then
    echo "fixup! commits remain after autosquash (unmatched target subject?)" >&2
    exit 1
  fi
  echo "autosquash: folded fixups into their targets"
else
  # The rebase exited non-zero. Distinguish a genuine fold CONFLICT (agent-resolvable)
  # from any other hard failure: a fold conflict has BOTH a rebase in progress AND
  # unmerged paths. Anything else (abnormal leftover, refused start, an empty-commit
  # stop) is a hard failure that must NOT burn an agent resolve loop.
  if [ -d "$(git rev-parse --git-path rebase-merge)" ] && [ -n "$(git ls-files -u)" ]; then
    # Fold conflict: hand off. Do NOT --abort; leave the rebase in progress with the
    # conflict markers, exactly as integrate-merge.sh leaves a tree for the integrator.
    mkdir -p .imp/change/state
    printf '%s\n' "$BEFORE_TREE" > .imp/change/state/autosquash-before-tree
    printf '%s\n' "$BEFORE" > .imp/change/state/autosquash-before-head
    echo "autosquash: fixup fold conflicted; handing to autosquash-resolve (rebase left in progress)" >&2
    exit 2
  fi
  # Hard failure: best-effort restore to pristine, then pause (unchanged behavior).
  git rebase --abort 2>/dev/null || true
  echo "autosquash rebase failed with no resolvable fold conflict; branch left intact for human resolution" >&2
  exit 1
fi
