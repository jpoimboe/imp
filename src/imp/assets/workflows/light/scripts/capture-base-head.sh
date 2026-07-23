#!/usr/bin/env bash
# Record the current-branch HEAD — the branch tip BEFORE imp adds any commits — so the
# reviewer has a stable diff base. Runs after front-door go-live, before the implementer.
# The SHA is the only thing on STDOUT (captured into the base_head workflow var).
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
git rev-parse HEAD
