#!/usr/bin/env bash
# Validate the implement phase's commits (spec §12). After the task-lead loop:
#   (1) the worktree is clean (the implementer committed everything it claimed);
#   (2) every non-merge commit since base carries the `Change: <slug>` trailer
#       (catches a `bash git commit` that bypassed the trailer-stamping tool);
#   (3) one-commit-per-task (upper bound): non-merge SOURCE commits since base
#       (touching paths outside imp/specs / imp/plans) must not exceed the number
#       of `## Task N` sections across the plan files. <= tolerates
#       verification-only tasks that commit nothing.
# Failure resumes task-lead with this message as feedback.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"

# (1) clean worktree
if [[ -n "$(git status --porcelain)" ]]; then
  echo "uncommitted changes remain — the implementer must commit all task work:" >&2
  git status --porcelain >&2
  exit 1
fi

# Base branch from the Change metadata (no engine env var carries it).
BASE=$(python3 -c "import json;print(json.load(open('.imp/change/change.json'))['base_branch'])")

# (2) every non-merge, non-fixup commit since base carries the trailer.
# `fixup!` commits are transient (Resolution 6) — autosquash folds them into a
# trailered target before review — so exclude them (--invert-grep --grep '^fixup!').
bad=""
while IFS= read -r h; do
  [[ -z "$h" ]] && continue
  t=$(git log -1 --format='%(trailers:key=Change,valueonly)' "$h" | head -n1)
  [[ "$t" == "$SLUG" ]] || bad+=" $h"
done < <(git rev-list --no-merges --invert-grep --grep='^fixup!' "$BASE"..HEAD)
if [[ -n "$bad" ]]; then
  echo "commit(s) missing 'Change: $SLUG' trailer (use the commit tool, not bash git commit):$bad" >&2
  exit 1
fi

# (3) one-commit-per-task (upper bound). SOURCE commits exclude the spec/plan
# docs AND the transient fixup! commits (folded by autosquash before review).
n_src=$(git rev-list --count --no-merges --invert-grep --grep='^fixup!' "$BASE"..HEAD -- . \
          ':(exclude)imp/specs/**' ':(exclude)imp/plans/**')
if [[ "$n_src" -lt 1 ]]; then
  echo "no source commits since $BASE — the implementer committed no task work" >&2
  exit 1
fi
n_tasks=$(cat imp/plans/${SLUG}-plan-*.md 2>/dev/null | grep -cE '^## Task [0-9]+' || true)
if [[ "${n_tasks:-0}" -le 0 ]]; then
  # Don't silently disable the cap: surface that no `## Task N` headers matched.
  echo "WARNING: no '## Task N' sections found in imp/plans/${SLUG}-plan-*.md; one-commit-per-task cap SKIPPED" >&2
elif [[ "$n_src" -gt "$n_tasks" ]]; then
  echo "one-commit-per-task violated: $n_src source commits for $n_tasks planned task(s); squash split commits via amend" >&2
  exit 1
fi
echo "ok: clean tree, all commits trailered, $n_src source commit(s) <= ${n_tasks:-0} task(s)"
