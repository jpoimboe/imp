#!/usr/bin/env bash
# Validate that a writer agent actually produced its output file.
# Usage: ./scripts/check-artifact.sh <path>
#   e.g. ./scripts/check-artifact.sh "imp/docs/specs/$IMP_CHANGE_SLUG.md"
#
# Catches the "agent claims status=Success but didn't actually write" failure.
# The path is the REAL worktree path the prompt wrote (no /artifacts/ indirection).
set -euo pipefail
TARGET="${1:?usage: check-artifact.sh <path>}"
if [[ ! -s "$TARGET" ]]; then
  echo "artifact missing or empty: $TARGET" >&2
  exit 1
fi
echo "ok ($(wc -c < "$TARGET") bytes): $TARGET"
