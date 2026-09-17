#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# deferred-check.sh — converge guard between quest-review and deferred-sweep. The
# sweep is a high-tier agent dispatch; when the quest's Deferred Followups list
# holds nothing to drain there is no work for it, so skip straight to the slow
# gate. Fails CLOSED: exit 1 is the ONLY skip signal, so any doubt — an unreadable
# path, an absent IMP_QUEST_DIR — dispatches the agent. The parse is deliberately
# dumb: it does not re-implement deferred-sweep's tag-aware drain logic, because a
# second parser drifting from the agent's is how items get silently dropped.
#
#   1 -> nothing to drain: deferred.md absent, no `## Deferred Followups` section,
#        or that section holds no `- [ ]` items -> converge-verify
#   0 -> an open item to drain, or any error reading the file -> deferred-sweep
# -e omitted: exit 1 is the skip signal; an -e-triggered exit reads as "nothing to drain"
set -uo pipefail
QUEST_DIR="${IMP_QUEST_DIR:-}"
[ -n "$QUEST_DIR" ] || { echo "no IMP_QUEST_DIR; dispatching the sweep" >&2; exit 0; }
FILE="$QUEST_DIR/deferred.md"
[ -e "$FILE" ] || { echo "ok: no $FILE — nothing to drain"; exit 1; }
{ [ -f "$FILE" ] && [ -r "$FILE" ]; } || { echo "cannot read $FILE; dispatching the sweep" >&2; exit 0; }

ITEM=$(awk '
  /^## / { in_section = ($0 ~ /^## Deferred Followups[[:space:]]*$/); next }
  in_section && /^- \[ \]/ { print "open"; exit }
' "$FILE") || { echo "awk failed on $FILE; dispatching the sweep" >&2; exit 0; }

if [ -z "$ITEM" ]; then
  echo "ok: no open '- [ ]' items under ## Deferred Followups — nothing to drain"
  exit 1
fi
echo "open deferred items found; dispatching the sweep"
exit 0
