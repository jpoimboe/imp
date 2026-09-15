"""Live deny-posture probes: spawn the SDK's bundled claude CLI under imp's exact
deny list and read the tool registry it advertises in the `init` frame. The
shared machinery is tests/cc_probe.py; the CLI-free regression tests that pin
the probe's fail-loud contract are in tests/test_cc_builtins_drift.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.providers.claude import ClaudeCodeAgentRunner
from tests.cc_probe import invocation, probe_advertised_tools, resolve_cli

pytestmark = pytest.mark.live("claude_code")


def test_no_builtin_escapes_imp_deny_posture(tmp_path: Path) -> None:
    opts = ClaudeCodeAgentRunner("m")._build_options(
        invocation(tmp_path), object(), ["bash"])
    advertised, _ = probe_advertised_tools(
        resolve_cli(opts), list(opts.disallowed_tools), tmp_path)

    escapees = advertised - {"ToolSearch"}
    assert not escapees, (
        f"Built-ins reachable under imp's deny posture: {sorted(escapees)}. "
        "The agent can call these from every claude_code step. Add them to "
        "ALL_CLAUDE_CODE_BUILTIN_TOOLS in _cc_builtins.py."
    )


def test_deny_posture_probe_is_live(tmp_path: Path) -> None:
    opts = ClaudeCodeAgentRunner("m")._build_options(
        invocation(tmp_path), object(), ["bash"])
    deny = [t for t in opts.disallowed_tools if t != "Bash"]
    advertised, stderr = probe_advertised_tools(resolve_cli(opts), deny, tmp_path)

    assert "Bash" in advertised, (
        "Probe is inert: expected Bash to be advertised when only ToolSearch "
        f"and Bash are exempt, got {sorted(advertised)}. The probe no longer "
        "reflects the CLI's init output — fix it before trusting the "
        "deny-posture guard."
        f"\nCLI stderr:\n{stderr}"
    )
