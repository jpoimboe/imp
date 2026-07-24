#!/usr/bin/env bash
# Shared pre-merge clearance predicate, SOURCED by integrate-rebase.sh (Part 1
# fast path) and assert-stamped.sh (Part 3 backstop). Not executed on its own —
# it only defines _cleared_for_merge.
#
# A tree is "cleared for merge" iff run-verify.sh verified it GREEN on a slow run
# (its tree SHA == last_green_tree_slow), OR it was an explicitly-tolerated red
# that ran on THIS exact tree (tolerated flag true AND tree SHA ==
# last_verified_tree_slow). Both stamp files live under $IMP_QUEST_DIR and are
# written by run-verify.sh (a no-op gate stamps green too, so an opt-out project
# is trivially cleared).
#
# Call from a condition context (`if _cleared_for_merge ...` / `if ! ...`) so a
# caller running under `set -e` is not aborted by the helper's non-zero return.

# _cleared_for_merge <tree-sha> <tolerated-flag, already lowercased>
#   0 -> cleared (verified green, or accepted red on this exact tree)
#   1 -> not cleared
_cleared_for_merge() {
  local tree="$1" tolerated="$2" d="${IMP_QUEST_DIR:?IMP_QUEST_DIR not set}"
  [ -f "$d/last_green_tree_slow" ] \
    && [ "$(cat "$d/last_green_tree_slow")" = "$tree" ] && return 0
  [ "$tolerated" = "true" ] \
    && [ -f "$d/last_verified_tree_slow" ] \
    && [ "$(cat "$d/last_verified_tree_slow")" = "$tree" ] && return 0
  return 1
}
