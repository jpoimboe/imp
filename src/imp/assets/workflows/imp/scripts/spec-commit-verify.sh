#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
CHANGE_DIR="${IMP_CHANGE_DIR:?IMP_CHANGE_DIR not set by engine}"
HANDLE="${IMP_CHANGE_HANDLE:?IMP_CHANGE_HANDLE not set by engine}"
ID="${IMP_CHANGE_ID:?IMP_CHANGE_ID not set by engine}"
WIP="refs/imp/journal-wip/${ID}"
LIVE="${CHANGE_DIR}/artifacts/spec/${HANDLE}.md"

if ! git rev-parse --verify --quiet "$WIP" >/dev/null; then
  echo "wip ref $WIP does not exist (spec not committed via artifact-commit.sh?)" >&2
  exit 1
fi
committed=$(git rev-parse --verify --quiet "${WIP}:spec/${HANDLE}.md") || {
  echo "$WIP tip does not carry spec/${HANDLE}.md" >&2; exit 1; }
live=$(git hash-object "$LIVE") || { echo "live spec $LIVE missing" >&2; exit 1; }
if [ "$committed" != "$live" ]; then
  echo "live spec $LIVE differs from the committed blob on $WIP" >&2
  exit 1
fi
echo "ok: $WIP carries spec/${HANDLE}.md matching $LIVE"
