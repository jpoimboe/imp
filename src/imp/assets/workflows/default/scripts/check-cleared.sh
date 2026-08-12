#!/usr/bin/env bash
# check-cleared.sh — read-only merge door between integration-reviewer and
# summarizer. The reviewer may accept a tree no slow gate ever passed (an
# environmental failure it diagnosed and repaired), so re-ask the shared
# predicate and send an uncleared tree back through the gate.
#   0 -> cleared (verified green, or an accepted red on THIS exact tree) -> summarizer
#   3 -> not cleared -> integration-verify
#   other -> could not determine (no project root, no git tree) -> pause
set -euo pipefail
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_here/lib-verify-clear.sh"
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}" || exit 1
: "${IMP_QUEST_DIR:?IMP_QUEST_DIR not set by engine}"
# Toleration flag from the workflow (bool var renders capitalized) -> lowercase.
# Portable normalization (tr), not the bash-4-only ${x,,}, so this parses on bash 3.2.
tolerated="$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')"
tree=$(git rev-parse 'HEAD^{tree}')

if _cleared_for_merge "$tree" "$tolerated"; then
  echo "ok: HEAD tree $tree cleared for merge"
  exit 0
fi
echo "HEAD tree $tree is neither verified green (last_green_tree_slow) nor an accepted red on this tree (last_verified_tree_slow + slow_verify_tolerated); re-running the integration gate" >&2
exit 3
