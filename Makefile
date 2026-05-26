# imp — development convenience targets.
#
# Run `make` or `make help` to see what's available.

# Default goal: print help. Tab-indented recipes are required by GNU make.
.DEFAULT_GOAL := help

# Use bash so we get pipefail and clearer error messages.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON   ?= python3
PYTEST   ?= $(PYTHON) -m pytest
RUFF     ?= $(PYTHON) -m ruff
MYPY     ?= $(PYTHON) -m mypy

# Where to source ANTHROPIC_API_KEY from for live tests, when it's not
# already exported in the shell. Override with KEY_FILE=... if you keep
# your key somewhere else.
KEY_FILE ?= /tmp/.imp-key


# ── primary targets ─────────────────────────────────────────────────────────

.PHONY: help
help: ## Print this help.
	@awk 'BEGIN {FS = ":.*?## "} \
	     /^[a-zA-Z0-9_.-]+:.*?## / { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 } \
	     /^# ── / { sub(/^# ── /, ""); sub(/ ──*$$/, ""); printf "\n\033[1m%s\033[0m\n", $$0 }' \
	     $(MAKEFILE_LIST)

.PHONY: install
install: ## Editable install of imp + dev deps (pytest, ruff, mypy).
	$(PYTHON) -m pip install -e '.[dev]' 2>/dev/null || $(PYTHON) -m pip install -e .

.PHONY: test
test: ## Fast test suite (skips live-API tests). What CI runs.
	$(PYTEST) -q

.PHONY: test-verbose
test-verbose: ## Same as `test` but with -v.
	$(PYTEST) -v

.PHONY: test-live
test-live: ## Run the live-API tests (real Anthropic calls; ~5-8 min, ~$0.50).
	@$(_LOAD_KEY) && IMP_LIVE_API=1 ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" \
	    timeout 1800 $(PYTEST) -v tests/test_end_to_end_live.py

.PHONY: test-live-quick
test-live-quick: ## Just the cheap live test (one step, ~3s, ~$0.001).
	@$(_LOAD_KEY) && IMP_LIVE_API=1 ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" \
	    $(PYTEST) -v tests/test_end_to_end_live.py::test_one_step_live

.PHONY: test-all
test-all: test test-live ## Full suite including live tests.

.PHONY: lint
lint: ## Check lint with ruff.
	$(RUFF) check .

.PHONY: lint-fix
lint-fix: ## Auto-fix ruff issues that can be fixed.
	$(RUFF) check --fix .

.PHONY: format
format: ## Format code with ruff.
	$(RUFF) format .

.PHONY: typecheck
typecheck: ## Run mypy on the source tree.
	$(MYPY) src

.PHONY: check
check: lint typecheck test ## Full pre-merge gate: lint + types + tests.

.PHONY: build
build: ## Build wheel + sdist into dist/.
	$(PYTHON) -m build

.PHONY: clean
clean: ## Remove build artifacts and caches.
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -not -path './.git/*' -delete


# ── private helpers ─────────────────────────────────────────────────────────

# Inline shell snippet: ensure ANTHROPIC_API_KEY is set in the current
# recipe's subshell. If empty, source from $(KEY_FILE). If that's missing
# too, print a clear error and exit non-zero. Use as `@$(_LOAD_KEY) && ...`.
define _LOAD_KEY
{ \
  if [[ -z "$${ANTHROPIC_API_KEY:-}" ]]; then \
    if [[ -r "$(KEY_FILE)" && -s "$(KEY_FILE)" ]]; then \
      export ANTHROPIC_API_KEY="$$(cat "$(KEY_FILE)")"; \
      echo "(loaded ANTHROPIC_API_KEY from $(KEY_FILE))" >&2; \
    else \
      echo "ERROR: ANTHROPIC_API_KEY not set and $(KEY_FILE) missing/empty." >&2; \
      echo "" >&2; \
      echo "Set it via one of:" >&2; \
      echo "  export ANTHROPIC_API_KEY=sk-ant-..." >&2; \
      echo "  echo 'sk-ant-...' > $(KEY_FILE) && chmod 600 $(KEY_FILE)" >&2; \
      echo "  make $(@) KEY_FILE=/path/to/your/keyfile" >&2; \
      exit 1; \
    fi; \
  fi; \
}
endef
