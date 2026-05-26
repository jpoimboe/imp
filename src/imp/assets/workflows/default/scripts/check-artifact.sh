#!/usr/bin/env bash
# Validate that a writer agent actually produced its artifact.
# Usage: ./scripts/check-artifact.sh <artifact-name>
#   e.g. ./scripts/check-artifact.sh spec    -> checks $IMP_ARTIFACTS_DIR/spec.md
#
# Catches the "agent claims status=Success but didn't actually write_file"
# failure mode (Bug N from V0 dogfood — design-writer lied).
set -euo pipefail
ARTIFACT="${1:?usage: check-artifact.sh <name>}"
PATH_TO_CHECK="${IMP_ARTIFACTS_DIR:?IMP_ARTIFACTS_DIR not set by engine}/${ARTIFACT}.md"
if [[ ! -s "$PATH_TO_CHECK" ]]; then
  echo "artifact missing or empty: $PATH_TO_CHECK" >&2
  exit 1
fi
echo "$ARTIFACT ok ($(wc -c < "$PATH_TO_CHECK") bytes)"
