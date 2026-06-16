#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
SLUG="${IMP_CHANGE_SLUG:?IMP_CHANGE_SLUG not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
WIP="refs/imp/journal-wip/${ID}"
LIVE=".imp/change/artifacts/spec/${SLUG}.md"

if ! git rev-parse --verify --quiet "$WIP" >/dev/null; then
  echo "wip ref $WIP does not exist (spec not committed via artifact-commit.sh?)" >&2
  exit 1
fi
committed=$(git rev-parse --verify --quiet "${WIP}:spec/${SLUG}.md") || {
  echo "$WIP tip does not carry spec/${SLUG}.md" >&2; exit 1; }
live=$(git hash-object "$LIVE") || { echo "live spec $LIVE missing" >&2; exit 1; }
if [ "$committed" != "$live" ]; then
  echo "live spec $LIVE differs from the committed blob on $WIP" >&2
  exit 1
fi
echo "ok: $WIP carries spec/${SLUG}.md matching $LIVE"
