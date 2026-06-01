#!/usr/bin/env bash
# Verify the spec commit (spec §12): HEAD must (a) touch imp/specs/<slug>.md and
# (b) carry a `Change: <slug>` trailer. Failure resumes spec-writer.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"
TARGET="imp/specs/${SLUG}.md"

# --root so a root commit (no parent) still reports its files; harmless otherwise.
if ! git diff-tree --root --no-commit-id --name-only -r HEAD | grep -qxF "$TARGET"; then
  echo "HEAD does not touch $TARGET" >&2
  exit 1
fi
if ! git log -1 --format='%(trailers:key=Change,valueonly)' | grep -qxF "$SLUG"; then
  echo "HEAD is missing the 'Change: $SLUG' trailer" >&2
  exit 1
fi
echo "ok: HEAD touches $TARGET with Change: $SLUG"
