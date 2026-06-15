#!/usr/bin/env bash
set -euo pipefail
cd "${IMP_PROJECT_ROOT:?IMP_PROJECT_ROOT not set by engine}"
KIND="${1:?usage: artifact-commit.sh <spec|plan|review>}"
exec imp artifact-commit "$KIND"
