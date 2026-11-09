#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# At-head integration: the quest is head of its merge-queue lane, so the base
# cannot move under it. Rebase the branch ONTO the base tip ONLY when the base
# moved (non-empty base-side incoming diff); otherwise skip the rebase and merge
# the branch as is. Linear history, no merge commit; the eventual branch->base
# merge is the only merge.
# On the clean (0), conflict (6), and unmoved-base (7) paths the bare pre-rebase fork
# SHA P is printed on STDOUT (and nothing else) so the workflow can capture it into
# the integration_fork var; the base tip B is always recoverable downstream as
# `git rev-parse "${BASE}^{commit}"`. All human-readable status goes to STDERR.
#   3  -> bad/missing base ref -> pause
#   5  -> dirty entry tree / leftover rebase / git refused to start -> pause
#   2  -> base unmoved AND this exact tree already cleared by an earlier slow pass or toleration: merge directly, NO review, no fork SHA
#   0  -> clean rebase (base moved, no conflict) -> validate-memory -> integration-verify -> integration-reviewer
#   6  -> rebase stopped on a conflict (left in progress) -> reconciler
#   7  -> base unmoved, tree NOT cleared (the normal first pass: converge runs no slow gate) -> validate-memory -> integration-verify; reviewer skipped on green, prints fork SHA
set -uo pipefail
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_here/lib-verify-clear.sh"
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || { echo "could not enter project root; pausing" >&2; exit 5; }
# Toleration flag from the workflow (bool var renders capitalized) -> lowercase.
# Portable normalization (tr), not the bash-4-only ${x,,}, so this parses on bash 3.2.
tolerated="$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')"
QUEST_DIR="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
QUEST_JSON="$QUEST_DIR/quest.json"
BASE=$("$IMP_PYTHON" -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_QUEST_DIR'],'quest.json')))['base_branch'])") || { echo "could not read base_branch from quest.json; pausing" >&2; exit 5; }
[ -n "$BASE" ] || { echo "base_branch is empty in quest.json; pausing" >&2; exit 5; }

# A bad base_branch makes `--is-ancestor` exit 128; verify the ref up front.
git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1 || { echo "base ref '$BASE' did not resolve (bad or missing base branch); pausing" >&2; exit 3; }
B=$(git rev-parse "${BASE}^{commit}") || { echo "could not resolve base tip for '$BASE'; pausing" >&2; exit 5; }

# Leftover in-progress rebase from a prior interrupted run -> pause (a human inspects).
if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
   || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "a rebase is already in progress at integration entry; pausing" >&2
  exit 5
fi

# Refuse a TRACKED-dirty entry tree so pre-existing changes aren't folded into a
# replayed commit. Scope to tracked only; untracked scratch is harmless.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "quest worktree has uncommitted TRACKED changes at integration entry; pausing" >&2
  exit 5
fi

# P is the pre-rebase fork point: the SHA the branch is currently planted on.
P=$("$IMP_PYTHON" -c "import json,os;print(json.load(open(os.path.join(os.environ['IMP_QUEST_DIR'],'quest.json')))['cur_base_head'])") || { echo "could not read cur_base_head from quest.json; pausing" >&2; exit 5; }
[ -n "$P" ] || { echo "cur_base_head is empty in quest.json; pausing" >&2; exit 5; }

# Base-side incoming diff: what base added since the fork point P. Empty => base
# has not moved relative to this branch -> no rebase: a cleared tree merges directly
# (2); an uncleared one goes to the slow verify (7). Leave cur_base_head untouched.
if git diff --quiet "$P" "$B"; then
  tree=$(git rev-parse 'HEAD^{tree}') || { echo "could not resolve HEAD tree; pausing" >&2; exit 5; }
  if _cleared_for_merge "$tree" "$tolerated"; then
    echo "base ($BASE) unmoved; tree already verified/accepted; merging directly" >&2
    exit 2 # -> summarizer (fast path)
  fi
  echo "base ($BASE) unmoved but tree not verified/accepted; routing to integration-verify" >&2
  echo "$P"                                 # fork SHA for the downstream reviewer's diff
  exit 7 # -> validate-memory -> integration-verify (the normal first pass)
fi

# Base moved: rebase the branch onto base. rerere replays any recorded resolution;
# --empty=drop drops a quest commit base already contains. The pre-rebase fork P
# is handed to the workflow on stdout for the integration_fork var.
if git -c rerere.enabled=true -c rerere.autoupdate=true rebase --empty=drop "$BASE" 1>&2; then
  # Clean replant complete: the branch now sits on B. Record cur_base_head := B.
  "$IMP_PYTHON" -c "import json,sys;p=sys.argv[1];m=json.load(open(p));m['cur_base_head']=sys.argv[2];json.dump(m,open(p,'w'),indent=2,sort_keys=True)" "$QUEST_JSON" "$B" || { echo "could not record cur_base_head after rebase; pausing" >&2; exit 5; }
  echo "clean rebase onto $BASE (no conflicts); handing to integration-reviewer" >&2
  echo "$P"
  exit 0
fi
if [ -d "$(git rev-parse --git-path rebase-merge)" ] \
   || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "rebase stopped on a conflict (left in progress); handing to reconciler" >&2
  echo "$P"
  exit 6
fi
echo "git refused to start the rebase (no rebase in progress); pausing for a human" >&2
exit 5
