# imp — development convenience targets.
#
# Run `make` or `make help` to see what's available.

# Default goal: print help. Tab-indented recipes are required by GNU make.
.DEFAULT_GOAL := help

# Use bash so we get pipefail and clearer error messages.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON   ?= $(CURDIR)/.venv/bin/python
PYTEST   ?= $(PYTHON) -m pytest
RUFF     ?= $(PYTHON) -m ruff
MYPY     ?= $(PYTHON) -m mypy
UV       ?= uv
# Empirical: serial ~282s -> parallel ~34s (-n auto, 12 workers); 20 runs identical; serial override: make test XDIST=
XDIST    ?= $(shell $(PYTHON) -c "import xdist" 2>/dev/null && echo "-n auto --dist loadgroup")
# Live tests are I/O-bound on real APIs, so parallelism is a big win — but the
# worker count is capped LOW (not -n auto). The thing that made -n 4 fragile was
# the global `timeout = 60` (signal method, pyproject.toml): the slow live tests
# run 30-45s each, so under concurrency they have little headroom before SIGALRM
# fires and kills a still-progressing call (spurious Timeout on a sync test, an
# event loop torn down mid-await on an async one). tests/conftest.py now gives
# every live test a 240s backstop so the timeout only catches a genuine hang, and
# the interrupt/resume tests carry generous (30-60s) internal SIGINT/pause
# deadlines. With those, -n 4 is stable and ~1.7x faster than -n 2 (full suite,
# 4 providers, 2026-06-28: -n 4 ~114s vs -n 2 ~200s).
#
# Don't go higher: -n 6+ OVER-subscribes a typical box (xdist workers + the
# spawned `claude`/`codex` subprocesses + their daemons all contend for CPU),
# starving the subprocess interrupt/resume tests until they miss their tool_call
# poll — measured flaky at -n 6 and failing at -n 8 on a 12-core machine, for
# only a marginal wall-clock gain. Residual flakes at -n 4 are model-level, not
# concurrency — re-run a single failure rather than treating it as a regression:
# gpt-4o-mini occasionally skips the scripted bash call and completes immediately.
#
# Override: LIVE_XDIST="-n 2" to lighten load; LIVE_XDIST= for serial debugging.
LIVE_XDIST ?= $(shell $(PYTHON) -c "import xdist" 2>/dev/null && echo "-n 4 --dist loadgroup")

# Where to source ANTHROPIC_API_KEY from for live tests, when it's not
# already exported in the shell. Override with KEY_FILE=... if you keep
# your key somewhere else.
KEY_FILE ?= /tmp/.imp-key


# ── primary targets ─────────────────────────────────────────────────────────

.PHONY: help
help: ## Print this help.
	@awk 'BEGIN {FS = ":.*?## "} \
	     /^[a-zA-Z0-9_.-]+:.*?## / { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' \
	     $(MAKEFILE_LIST)

# `uv sync` installs exactly what uv.lock pins into this worktree's .venv; nothing re-resolves.
.PHONY: sync-env
sync-env: ## Create/reconcile this worktree's .venv to uv.lock EXACTLY (fixes `make depcheck` drift).
	$(UV) sync --all-extras

# A target, not a parse-time $(error): sync-env and help must work on a fresh clone.
.PHONY: require-venv
require-venv:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
	  echo "ERROR: no Python at $(PYTHON)" >&2; \
	  echo "create it with: make sync-env" >&2; \
	  exit 1; \
	}

# Scope a run: make test PYTEST_ARGS="tests/foo.py -k name"
.PHONY: test
test: require-venv ## Fast test suite (skips live provider tests). What CI runs.
	$(PYTEST) $(XDIST) -q $(PYTEST_ARGS)

.PHONY: test-verbose
test-verbose: require-venv ## Same as `test` but with -v.
	$(PYTEST) $(XDIST) -v $(PYTEST_ARGS)

_LIVE_TEST_FILES := \
  tests/test_default_workflow_e2e_live.py \
  tests/test_default_workflow_e2e_live_openai.py \
  tests/test_default_workflow_e2e_live_claude.py \
  tests/test_mid_step_resume_live.py \
  tests/test_openai_gate_feedback_resume_live.py \
  tests/test_resume_all_shapes_live.py \
  tests/test_native_session_resume_live.py \
  tests/test_relay_hibernation_live.py \
  tests/test_codex_provider_live.py \
  tests/test_codex_started_keying_live.py \
  tests/test_codex_context_reading_live.py \
  tests/test_parallel_elicitation_live.py \
  tests/test_cc_builtins_drift_live.py \
  tests/test_claude_tier_defaults_live.py

.PHONY: test-live
test-live: require-venv ## Run tests needing a real provider (--run-live); providers whose toolchain/creds are present run, the rest skip.
	@$(_LOAD_LIVE_KEYS) && \
	  timeout --foreground 1800 $(PYTEST) $(LIVE_XDIST) --run-live -v -m "not codex_parallel" $(_LIVE_TEST_FILES)

.PHONY: test-live-verbose
test-live-verbose: require-venv ## test-live but with live progress + raw PTY output streamed to your terminal.
	@$(_LOAD_LIVE_KEYS) && \
	  timeout --foreground 1800 $(PYTEST) --run-live -s -v -m "not codex_parallel" $(_LIVE_TEST_FILES)

.PHONY: test-live-quick
test-live-quick: require-venv ## One-step live test for each provider whose toolchain/creds are present.
	@$(_LOAD_LIVE_KEYS) && \
	  $(PYTEST) --run-live -v \
	    tests/test_default_workflow_e2e_live.py \
	    tests/test_default_workflow_e2e_live_openai.py \
	    tests/test_default_workflow_e2e_live_claude.py

.PHONY: test-live-codex-parallel
test-live-codex-parallel: require-venv ## Run the codex parallel-MCP concurrency probe (XFAIL on codex builds that serialize MCP dispatch).
	@$(_LOAD_LIVE_KEYS) && \
	  $(PYTEST) --run-live -v -m codex_parallel \
	    tests/test_codex_started_keying_live.py

.PHONY: test-all
test-all: require-venv test test-live ## Full suite including live tests.

.PHONY: lint
lint: require-venv ## Check lint with ruff.
	$(RUFF) check .

.PHONY: lint-fix
lint-fix: require-venv ## Auto-fix ruff issues that can be fixed.
	$(RUFF) check --fix .

.PHONY: format
format: require-venv ## Format code with ruff.
	$(RUFF) format .

.PHONY: typecheck
typecheck: require-venv ## Run mypy on the source tree.
	$(MYPY) src

.PHONY: lockcheck
lockcheck: ## Verify uv.lock is in sync with pyproject.toml.
	$(UV) lock --check

.PHONY: depcheck
depcheck: require-venv ## Verify the test interpreter has every dependency pyproject declares.
	$(PYTHON) scripts/check_deps.py

.PHONY: check
check: require-venv depcheck lint typecheck lockcheck test ## Full pre-merge gate: deps + lint + types + lockfile + tests.

.PHONY: build
build: require-venv ## Build wheel + sdist into dist/.
	$(PYTHON) -m build

.PHONY: clean
clean: ## Remove build artifacts and caches.
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -not -path './.git/*' -delete

.PHONY: snap-ui
snap-ui: require-venv ## Render the full run-journal fixture to a PNG and open it. Extra: SNAP_ARGS="--limit 40".
	$(PYTHON) scripts/snap_imp_ui.py stream --open $(SNAP_ARGS)

.PHONY: snap-ui-light
snap-ui-light: require-venv ## Same as snap-ui but the light theme on a near-white surface.
	$(PYTHON) scripts/snap_imp_ui.py stream --theme light --out /tmp/imp_ui/stream-light.png --open $(SNAP_ARGS)

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

# Best-effort load of provider key files into env vars for live tests. For each
# of ANTHROPIC_API_KEY / OPENAI_API_KEY: if unset, export it from the first
# readable key file ($(KEY_FILE) or ~/.anthropic-key for anthropic; ~/.openai-key
# for openai). NEVER fails on a missing key or CLI — capability detection at
# collection skips a provider whose credentials/toolchain are absent. Use as
# `@$(_LOAD_LIVE_KEYS) && ...`.
define _LOAD_LIVE_KEYS
{ \
  if [[ -z "$${ANTHROPIC_API_KEY:-}" ]]; then \
    if [[ -r "$(KEY_FILE)" && -s "$(KEY_FILE)" ]]; then \
      export ANTHROPIC_API_KEY="$$(cat "$(KEY_FILE)")"; \
    elif [[ -r "$$HOME/.anthropic-key" && -s "$$HOME/.anthropic-key" ]]; then \
      export ANTHROPIC_API_KEY="$$(cat "$$HOME/.anthropic-key")"; \
    fi; \
  fi; \
  if [[ -z "$${OPENAI_API_KEY:-}" ]]; then \
    if [[ -r "$$HOME/.openai-key" && -s "$$HOME/.openai-key" ]]; then \
      export OPENAI_API_KEY="$$(cat "$$HOME/.openai-key")"; \
    fi; \
  fi; \
}
endef
