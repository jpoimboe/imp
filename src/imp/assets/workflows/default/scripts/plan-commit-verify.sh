#!/usr/bin/env bash
# Verify the newest plan commit. The glob is authoritative for N: the newest
# imp/plans/<slug>-plan-N.md is the current plan; a non-fixup commit must touch
# it. (The `Change:` trailer is stamped later, in the review phase, so it is not
# checked here.) Failure resumes plan-writer.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"

# Newest plan by numeric N (anchored …-plan-<N>.md so a -review-style name can't
# match — defense even though reviews are not files in this design).
NEWEST=""
best=-1
for f in imp/plans/${SLUG}-plan-*.md; do
  [[ -e "$f" ]] || continue
  n=${f##*-plan-}; n=${n%.md}
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  if (( n > best )); then best=$n; NEWEST=$(basename "$f"); fi
done
if [[ -z "$NEWEST" ]]; then
  echo "no imp/plans/${SLUG}-plan-N.md found" >&2
  exit 1
fi
TARGET="imp/plans/${NEWEST}"
# Fixup-aware (Resolution 6): plan revisions (rework AND deviation) are recorded
# as trailer-less `fixup!` commits, so HEAD may be a fixup of the plan. Validate
# the most-recent NON-fixup commit that touches the plan file — that is the
# original plan commit, whether it is HEAD or buried under a fixup
# and/or task commits. (rev-list with a pathspec reports root commits correctly,
# so no --root needed here.)
PLAN_COMMIT=$(git rev-list --no-merges --invert-grep --grep='^fixup!' -1 HEAD -- "$TARGET")
if [[ -z "$PLAN_COMMIT" ]]; then
  echo "no non-fixup commit touches the newest plan $TARGET (plan not committed?)" >&2
  exit 1
fi
echo "ok: $TARGET committed in $PLAN_COMMIT"
