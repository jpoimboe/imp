"""Drift guard: ALL_CLAUDE_CODE_BUILTIN_TOOLS must match the installed CLI.

Mechanism: spawn `claude --print --output-format stream-json --verbose
--input-format stream-json` with a minimal prompt and observe the `init`
system message — claude advertises its tool registry there.

If `claude` is missing (CI without the CLI), skip. The point of the guard is
to fire when the CLI IS available and a new built-in has appeared.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from imp.providers._cc_builtins import ALL_CLAUDE_CODE_BUILTIN_TOOLS


def test_builtin_tools_match_cli() -> None:
    cli = shutil.which("claude")
    if cli is None:
        pytest.skip("`claude` CLI not on PATH")

    # Minimal stream-json input: one short user prompt that terminates without
    # tool use. The `init` system message arrives BEFORE the model starts the
    # conversation, so any small prompt suffices. `--max-turns 0` is rejected
    # by the CLI (minimum is 1), so use 1 with a self-terminating prompt.
    user_msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "say ok"},
    })

    proc = subprocess.run(
        [
            cli, "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--setting-sources", "",
            "--no-session-persistence",
            "--max-turns", "1",
        ],
        input=user_msg + "\n",
        capture_output=True, text=True, timeout=20,
    )

    advertised: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            # `tools` field on init may be a list of tool names or descriptors.
            tools = msg.get("tools") or msg.get("data", {}).get("tools") or []
            for t in tools:
                if isinstance(t, str):
                    advertised.add(t)
                elif isinstance(t, dict) and "name" in t:
                    advertised.add(t["name"])
            break

    if not advertised:
        pytest.skip(
            "claude CLI did not advertise tools via init message; CLI format "
            "may have changed. Investigate `claude --print ... --max-turns 0` "
            "output and update this test."
        )

    declared = set(ALL_CLAUDE_CODE_BUILTIN_TOOLS)
    missing_in_constant = advertised - declared
    extra_in_constant = declared - advertised

    assert not missing_in_constant, (
        f"claude advertises built-ins not in ALL_CLAUDE_CODE_BUILTIN_TOOLS: "
        f"{sorted(missing_in_constant)}. Update _cc_builtins.py."
    )
    # Extras in the constant are non-fatal (claude removed one) but worth a
    # warning — print and let the test pass.
    if extra_in_constant:
        print(
            f"WARNING: ALL_CLAUDE_CODE_BUILTIN_TOOLS lists tools not in the "
            f"current CLI: {sorted(extra_in_constant)}. Safe to remove."
        )
