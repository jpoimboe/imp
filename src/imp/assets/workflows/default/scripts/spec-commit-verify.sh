#!/usr/bin/env bash
# Verify the spec commit: HEAD must touch imp/specs/<slug>.md. (The `Change:`
# trailer is stamped later, in the review phase, so it is not checked here.)
# Failure resumes spec-writer.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"
TARGET="imp/specs/${SLUG}.md"

# --root so a root commit (no parent) still reports its files; harmless otherwise.
if ! git diff-tree --root --no-commit-id --name-only -r HEAD | grep -qxF "$TARGET"; then
  echo "HEAD does not touch $TARGET" >&2
  exit 1
fi

# Clean-spec-path check (private worktree): no uncommitted/staged/untracked spec
# changes may survive. Without `--ignored`, git status omits gitignored files
# (ignored == declared acceptable); it catches modified, staged, and
# untracked-unignored states. `--porcelain` just selects the stable output format.
if [ -n "$(git status --porcelain -- imp/specs/)" ]; then
  echo "uncommitted or untracked changes under imp/specs/ (must be committed or removed):" >&2
  git status --porcelain -- imp/specs/ >&2
  exit 1
fi

echo "ok: HEAD touches $TARGET and imp/specs/ is clean"
