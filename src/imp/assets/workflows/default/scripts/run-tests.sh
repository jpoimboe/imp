#!/usr/bin/env bash
# Run the project's test suite. Convention: project provides `make test`
# or `pytest` works. Falls through to a no-op pass if neither exists so
# new projects without tests don't block.
set -euo pipefail
if [[ -f Makefile ]] && grep -q '^test:' Makefile; then
  make test
elif command -v pytest >/dev/null 2>&1 && [[ -d tests ]]; then
  pytest -q
else
  echo "no tests found; passing by default"
fi
