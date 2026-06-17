"""Grep guard: enforce the public-attribute contract for V1 Provider/Runner.

Plan 1 review #7 added `Provider.model`. Plan 2 ships two more runners that
share the same pattern: tests/engine/runners must never reach into the
runner's private state (`_client`, `_client_factory`, `_model`). Failures
here mean someone copy-pasted a fixture that monkey-patches a private
attribute instead of using the constructor's public kwargs.

The runners are: AnthropicAgentRunner, OpenAIAgentRunner, ClaudeCodeAgentRunner,
CodexAgentRunner. Their public surface is the AgentRunner protocol (.run,
.interrupt) plus .model (per Provider protocol). Anything else is private.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PRIVATE_PATTERNS = (
    r"provider\._model",
    r"runner\._model",
    r"\._client_factory\b",
    r"\._client\b",
    r"self\._model\b",
    r"\._model\b",
)


def test_no_private_reachthrough_across_providers() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    this_file = Path(__file__).name
    pattern = "|".join(PRIVATE_PATTERNS)
    proc = subprocess.run(
        # This guard enforces a *Python* contract, so scan only Python source.
        ["grep", "-rn", "-E",
         "--include", "*.py",
         "--exclude", this_file,
         pattern,
         str(repo_root / "src/imp"), str(repo_root / "tests")],
        capture_output=True, text=True, check=False,
    )
    # grep returns 1 when no matches (good); 0 means we found offenders.
    if proc.returncode != 0:
        return  # no matches → pass

    # Legitimate sites: each runner/provider's own __init__/_get_client where
    # the private attrs are *defined*. Everything else fails the test.
    allowed_substrings = (
        "src/imp/providers/anthropic.py",
        "src/imp/providers/openai.py",
        "src/imp/providers/claude_code.py",
        "src/imp/providers/codex.py",
        "src/imp/providers/codex_runner.py",
        "src/imp/cli/session_controls.py",
    )
    offending = [
        line for line in proc.stdout.splitlines()
        if line.strip() and not any(s in line for s in allowed_substrings)
    ]
    assert not offending, (
        "Private attribute reach-through detected (use the public constructor "
        "kwarg or provider.model instead):\n" + "\n".join(offending)
    )
