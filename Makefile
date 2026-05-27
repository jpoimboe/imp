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
	     /^[a-zA-Z0-9_.-]+:.*?## / { printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2 } \
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

_LIVE_TEST_FILES := \
  tests/test_default_workflow_e2e_live.py \
  tests/test_default_workflow_e2e_live_openai.py \
  tests/test_default_workflow_e2e_live_claude_code.py \
  tests/test_default_workflow_pty_live.py \
  tests/test_mid_step_resume_live.py

.PHONY: test-live
test-live: ## Run live-API tests for providers opted in via IMP_LIVE_PROVIDERS.
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_RUN_LIVE_TESTS=1 IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" \
	    timeout --foreground 1800 $(PYTEST) -v $(_LIVE_TEST_FILES)

.PHONY: test-live-verbose
test-live-verbose: ## test-live but with live progress + raw PTY output streamed to your terminal.
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_RUN_LIVE_TESTS=1 IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" \
	  IMP_PTY_TRACE=1 \
	    timeout --foreground 1800 $(PYTEST) -s -v $(_LIVE_TEST_FILES)

.PHONY: test-live-quick
test-live-quick: ## One-step live test for the first opted-in provider only.
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" \
	    $(PYTEST) -v \
	      tests/test_default_workflow_e2e_live.py \
	      tests/test_default_workflow_e2e_live_openai.py \
	      tests/test_default_workflow_e2e_live_claude_code.py

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

# Validate IMP_LIVE_PROVIDERS is set, every token is a recognized provider,
# and the matching auth is present (loading key files into env vars as the
# legacy _LOAD_KEY did). Use as `@$(_REQUIRE_PROVIDERS) && ...`.
define _REQUIRE_PROVIDERS
{ \
  if [[ -z "$${IMP_LIVE_PROVIDERS:-}" ]]; then \
    echo "ERROR: IMP_LIVE_PROVIDERS is not set." >&2; \
    echo "" >&2; \
    echo "Set it to a comma-separated list of providers you have auth for:" >&2; \
    echo "  IMP_LIVE_PROVIDERS=anthropic       (needs ANTHROPIC_API_KEY or ~/.anthropic-key)" >&2; \
    echo "  IMP_LIVE_PROVIDERS=openai          (needs OPENAI_API_KEY or ~/.openai-key)" >&2; \
    echo "  IMP_LIVE_PROVIDERS=claude_code     (needs \`claude\` CLI on PATH)" >&2; \
    echo "  IMP_LIVE_PROVIDERS=anthropic,openai,claude_code  (combine as desired)" >&2; \
    echo "" >&2; \
    echo "Note: per-provider one-step tests run for every provider listed." >&2; \
    echo "      The full PTY workflow test uses only the FIRST provider in the list." >&2; \
    exit 1; \
  fi; \
  IFS=',' read -r -a _provs <<< "$${IMP_LIVE_PROVIDERS}"; \
  for prov in "$${_provs[@]}"; do \
    case "$$prov" in \
      anthropic) \
        if [[ -z "$${ANTHROPIC_API_KEY:-}" ]]; then \
          if [[ -r "$(KEY_FILE)" && -s "$(KEY_FILE)" ]]; then \
            export ANTHROPIC_API_KEY="$$(cat "$(KEY_FILE)")"; \
          elif [[ -r "$$HOME/.anthropic-key" && -s "$$HOME/.anthropic-key" ]]; then \
            export ANTHROPIC_API_KEY="$$(cat "$$HOME/.anthropic-key")"; \
          else \
            echo "ERROR: provider 'anthropic' opted in but no ANTHROPIC_API_KEY / ~/.anthropic-key." >&2; \
            exit 1; \
          fi; \
        fi ;; \
      openai) \
        if [[ -z "$${OPENAI_API_KEY:-}" ]]; then \
          if [[ -r "$$HOME/.openai-key" && -s "$$HOME/.openai-key" ]]; then \
            export OPENAI_API_KEY="$$(cat "$$HOME/.openai-key")"; \
          else \
            echo "ERROR: provider 'openai' opted in but no OPENAI_API_KEY / ~/.openai-key." >&2; \
            exit 1; \
          fi; \
        fi ;; \
      claude_code) \
        if ! command -v claude >/dev/null 2>&1; then \
          echo "ERROR: provider 'claude_code' opted in but \`claude\` CLI not on PATH." >&2; \
          exit 1; \
        fi ;; \
      *) \
        echo "ERROR: unknown provider '$$prov' in IMP_LIVE_PROVIDERS (valid: anthropic, openai, claude_code)." >&2; \
        exit 1 ;; \
    esac; \
  done; \
}
endef
