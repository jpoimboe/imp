#!/usr/bin/env bash
# Run the project's test suite. Convention: project provides `make test`
# or `pytest` works. Falls through to a no-op pass if neither exists so
# new projects without tests don't block.
#
# Script gates run with cwd=$IMP_WORKFLOW_DIR; cd into the project tree
# before invoking the test runner.
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
if [[ -f Makefile ]] && grep -q '^test:' Makefile; then
  make test
elif command -v pytest >/dev/null 2>&1 && [[ -d tests ]]; then
  # Use timeout so a hung suite doesn't block the workflow forever.
  timeout 300 pytest -q
else
  echo "no tests found; passing by default"
fi
