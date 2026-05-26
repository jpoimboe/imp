#!/usr/bin/env bash
# Run the project's test suite. Convention: project provides `make test`
# or `python -m pytest` works.
#
# Script gates run with cwd=$IMP_WORKFLOW_DIR; cd into the project tree
# before invoking the test runner.
#
# Decision tree:
#   - Makefile with `test:` target → `make test`
#   - tests/ dir + pytest importable → `python3 -m pytest`
#     (use `python -m pytest` not bare `pytest` because PATH may not
#      contain pytest's bin dir in subprocess envs — Bug Q)
#   - tests/ dir but pytest NOT importable → fail loud (broken setup)
#   - no tests/ dir AND no Makefile test target → pass (new project)
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"

PY="${IMP_PYTHON:-python3}"

if [[ -f Makefile ]] && grep -q '^test:' Makefile; then
  exec timeout 300 make test
fi

if [[ -d tests ]]; then
  if "$PY" -c "import pytest" 2>/dev/null; then
    exec timeout 300 "$PY" -m pytest -q
  else
    echo "tests/ dir exists but pytest not importable under $PY" >&2
    echo "(install with: $PY -m pip install pytest)" >&2
    exit 2
  fi
fi

echo "no tests found; passing by default (no Makefile target, no tests/ dir)"
