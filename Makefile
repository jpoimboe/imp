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
# (The old codex "tool not exposed" refusal is fixed — _build_prompt tells the
# model to load its mcp__imp__* tools via codex's tool_search first, ~0% now.)
#
# Override: LIVE_XDIST="-n 2" to lighten load; LIVE_XDIST= for serial debugging.
LIVE_XDIST ?= $(shell $(PYTHON) -c "import xdist" 2>/dev/null && echo "-n 4 --dist loadgroup")

# Subprocess tests (`python -m imp ...`, cold-import checks) resolve `imp` via
# the editable install — which inside a git worktree points at the MAIN checkout,
# not this tree, so they'd run stale code. Pin spawned subprocesses to THIS
# checkout's src. Absolute ($(CURDIR)) because a relative path breaks tests that
# chdir to a tmpdir; prepended so an ambient PYTHONPATH is preserved after it. A
# no-op in the main checkout (same dir the editable install already resolves).
PYTEST_PYTHONPATH := PYTHONPATH=$(CURDIR)/src$${PYTHONPATH:+:$$PYTHONPATH}

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

.PHONY: install
install: ## Editable install of imp + dev deps (pytest, ruff, mypy).
	$(PYTHON) -m pip install -e '.[dev]' 2>/dev/null || $(PYTHON) -m pip install -e .

# Scope a run: make test PYTEST_ARGS="tests/foo.py -k name"
.PHONY: test
test: ## Fast test suite (skips live-API tests). What CI runs.
	IMP_LIVE_PROVIDERS= $(PYTEST_PYTHONPATH) $(PYTEST) $(XDIST) -q $(PYTEST_ARGS)

.PHONY: test-verbose
test-verbose: ## Same as `test` but with -v.
	IMP_LIVE_PROVIDERS= $(PYTEST_PYTHONPATH) $(PYTEST) $(XDIST) -v $(PYTEST_ARGS)

_LIVE_TEST_FILES := \
  tests/test_default_workflow_e2e_live.py \
  tests/test_default_workflow_e2e_live_openai.py \
  tests/test_default_workflow_e2e_live_claude_code.py \
  tests/test_mid_step_resume_live.py \
  tests/test_openai_gate_feedback_resume_live.py \
  tests/test_resume_all_shapes_live.py \
  tests/test_native_session_resume_live.py \
  tests/test_relay_hibernation_live.py \
  tests/test_codex_provider_live.py \
  tests/test_codex_started_keying_live.py \
  tests/test_parallel_concurrency_live.py \
  tests/test_parallel_elicitation_live.py

.PHONY: test-live
test-live: ## Run live-API tests for providers opted in via IMP_LIVE_PROVIDERS.
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" $(PYTEST_PYTHONPATH) \
	    timeout --foreground 1800 $(PYTEST) $(LIVE_XDIST) -v -m "not codex_parallel" $(_LIVE_TEST_FILES)

.PHONY: test-live-verbose
test-live-verbose: ## test-live but with live progress + raw PTY output streamed to your terminal.
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" $(PYTEST_PYTHONPATH) \
	    timeout --foreground 1800 $(PYTEST) -s -v -m "not codex_parallel" $(_LIVE_TEST_FILES)

.PHONY: test-live-quick
test-live-quick: ## One-step live test for the first opted-in provider only.
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" $(PYTEST_PYTHONPATH) \
	    $(PYTEST) -v \
	      tests/test_default_workflow_e2e_live.py \
	      tests/test_default_workflow_e2e_live_openai.py \
	      tests/test_default_workflow_e2e_live_claude_code.py

.PHONY: test-live-codex-parallel
test-live-codex-parallel: ## Run the codex parallel-MCP concurrency probe (XFAIL on codex builds that serialize MCP dispatch).
	@$(_REQUIRE_PROVIDERS) && \
	  IMP_LIVE_PROVIDERS="$$IMP_LIVE_PROVIDERS" $(PYTEST_PYTHONPATH) \
	    $(PYTEST) -v -m codex_parallel \
	      tests/test_parallel_concurrency_live.py \
	      tests/test_codex_started_keying_live.py

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

.PHONY: lockcheck
lockcheck: ## Verify uv.lock is in sync with pyproject.toml.
	$(UV) lock --check

.PHONY: depcheck
depcheck: ## Verify the test interpreter has every dependency pyproject declares.
	$(PYTHON) scripts/check_deps.py

.PHONY: check
check: depcheck lint typecheck lockcheck test ## Full pre-merge gate: deps + lint + types + lockfile + tests.

.PHONY: build
build: ## Build wheel + sdist into dist/.
	$(PYTHON) -m build

.PHONY: clean
clean: ## Remove build artifacts and caches.
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -not -path './.git/*' -delete

.PHONY: snap-ui
snap-ui: ## Render the full run-journal fixture to a PNG and open it. Extra: SNAP_ARGS="--limit 40".
	$(PYTEST_PYTHONPATH) $(PYTHON) scripts/snap_imp_ui.py stream --open $(SNAP_ARGS)

.PHONY: snap-ui-light
snap-ui-light: ## Same as snap-ui but the light theme on a near-white surface.
	$(PYTEST_PYTHONPATH) $(PYTHON) scripts/snap_imp_ui.py stream --theme light --out /tmp/imp_ui/stream-light.png --open $(SNAP_ARGS)


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
    echo "  IMP_LIVE_PROVIDERS=codex           (needs \`codex\` CLI on PATH + openai_codex_sdk)" >&2; \
    echo "  IMP_LIVE_PROVIDERS=anthropic,openai,claude_code,codex  (combine as desired)" >&2; \
    echo "" >&2; \
    echo "Note: per-provider one-step tests run for every provider listed." >&2; \
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
      codex) \
        if ! command -v codex >/dev/null 2>&1; then \
          echo "ERROR: provider 'codex' opted in but \`codex\` CLI not on PATH." >&2; \
          exit 1; \
        fi ;; \
      *) \
        echo "ERROR: unknown provider '$$prov' in IMP_LIVE_PROVIDERS (valid: anthropic, openai, claude_code, codex)." >&2; \
        exit 1 ;; \
    esac; \
  done; \
}
endef
