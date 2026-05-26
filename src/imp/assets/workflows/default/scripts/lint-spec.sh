#!/usr/bin/env bash
# Minimal spec lint: spec artifact exists and is non-empty.
set -euo pipefail
SPEC="${IMP_ARTIFACTS_DIR:?IMP_ARTIFACTS_DIR not set by engine}/spec.md"
if [[ ! -s "$SPEC" ]]; then
  echo "spec missing or empty: $SPEC" >&2
  exit 1
fi
echo "spec ok"
