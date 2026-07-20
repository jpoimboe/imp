"""Unit tests for the --run-live gating mechanism in tests/_live.py and the
tests/conftest.py collection hook. These are ordinary (non-live) unit tests —
the `test_live_` filename prefix keeps them out of the live-file convention."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._live import KNOWN_PROVIDERS, RUN_LIVE_ALL, parse_run_live

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_parse_run_live_absent_returns_none():
    assert parse_run_live(None) is None


def test_parse_run_live_bare_returns_all_providers():
    assert parse_run_live(RUN_LIVE_ALL) == frozenset(KNOWN_PROVIDERS)
    assert set(KNOWN_PROVIDERS) == {"anthropic", "openai", "claude_code", "codex"}


def test_parse_run_live_subset_parses_named_providers():
    assert parse_run_live("claude_code,openai") == frozenset({"claude_code", "openai"})


def test_parse_run_live_unknown_provider_raises_usage_error():
    with pytest.raises(pytest.UsageError) as exc:
        parse_run_live("bogus")
    msg = str(exc.value)
    assert "bogus" in msg
    assert all(p in msg for p in KNOWN_PROVIDERS)  # names the valid providers


def test_anthropic_key_prefers_key_file_over_env(monkeypatch, tmp_path):
    (tmp_path / ".anthropic-key").write_text("file-key\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    from tests._live import anthropic_key
    assert anthropic_key() == "file-key"


def test_anthropic_key_falls_back_to_env_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.anthropic-key present
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    from tests._live import anthropic_key
    assert anthropic_key() == "env-key"


def test_anthropic_key_none_when_neither(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from tests._live import anthropic_key
    assert anthropic_key() is None


def test_openai_key_prefers_key_file_over_env(monkeypatch, tmp_path):
    (tmp_path / ".openai-key").write_text("file-key\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    from tests._live import openai_key
    assert openai_key() == "file-key"


def test_provider_available_claude_code_missing_cli(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live.shutil, "which", lambda _name: None)
    assert live.provider_available("claude_code") == "claude CLI not on PATH"


def test_provider_available_claude_code_present(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/claude")
    assert live.provider_available("claude_code") is None


def test_provider_available_codex_missing_sdk(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(live.importlib.util, "find_spec", lambda _name: None)
    assert live.provider_available("codex") == "openai_codex_sdk not importable"


def test_provider_available_anthropic_missing_key(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "anthropic_key", lambda: None)
    reason = live.provider_available("anthropic")
    assert reason is not None and "anthropic" in reason.lower() and "ANTHROPIC_API_KEY" in reason


def test_provider_available_anthropic_present(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "anthropic_key", lambda: "real-key")
    assert live.provider_available("anthropic") is None


def test_live_skip_reason_no_flag_skips_all():
    from tests._live import live_skip_reason
    assert live_skip_reason(None, "anthropic") == "live-API test — pass --run-live to run"


def test_live_skip_reason_provider_not_selected():
    from tests._live import live_skip_reason
    reason = live_skip_reason(frozenset({"openai"}), "anthropic")
    assert reason == "anthropic not selected by --run-live"


def test_live_skip_reason_selected_but_unavailable(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "provider_available", lambda _p: "claude CLI not on PATH")
    assert (
        live.live_skip_reason(frozenset({"claude_code"}), "claude_code")
        == "claude CLI not on PATH"
    )


def test_live_skip_reason_selected_and_available_runs(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "provider_available", lambda _p: None)
    assert live.live_skip_reason(frozenset({"anthropic"}), "anthropic") is None


from tests._live import RUN_LIVE_ALL as _ALL  # noqa: E402


class _StubItem:
    """Minimal stand-in for pytest.Item for exercising the collection hook."""

    def __init__(self, fspath, live_provider=None):
        self.fspath = fspath
        self._markers = {}
        if live_provider is not None:
            self._markers["live"] = pytest.mark.live(live_provider).mark
        self.added = []

    def get_closest_marker(self, name):
        for deco in self.added:
            if deco.mark.name == name:
                return deco.mark
        return self._markers.get(name)

    def add_marker(self, deco):
        self.added.append(deco)


class _StubConfig:
    def __init__(self, run_live):
        self._run_live = run_live

    def getoption(self, name):
        assert name == "--run-live"
        return self._run_live


def _skip_reasons(item):
    return [d.mark.kwargs["reason"] for d in item.added if d.mark.name == "skip"]


def test_hook_no_flag_skips_live_marked_test():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_x_live.py", live_provider="anthropic")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    assert "live-API test — pass --run-live to run" in _skip_reasons(item)


def test_hook_adds_raised_timeout_to_live_file():
    from tests.conftest import _LIVE_TEST_TIMEOUT, pytest_collection_modifyitems
    item = _StubItem("tests/test_x_live.py", live_provider="anthropic")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    timeouts = [d for d in item.added if d.mark.name == "timeout"]
    assert timeouts and timeouts[0].mark.args[0] == _LIVE_TEST_TIMEOUT


def test_hook_runs_selected_available_provider(monkeypatch):
    import tests._live as live
    from tests.conftest import pytest_collection_modifyitems
    monkeypatch.setattr(live, "provider_available", lambda _p: None)
    item = _StubItem("tests/test_x_live.py", live_provider="claude_code")
    pytest_collection_modifyitems(_StubConfig(_ALL), [item])
    assert _skip_reasons(item) == []


def test_hook_ignores_non_live_item():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_ordinary.py", live_provider=None)
    pytest_collection_modifyitems(_StubConfig(None), [item])
    assert item.added == []


def test_hook_unknown_provider_flag_raises_usage_error():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_x_live.py", live_provider="anthropic")
    with pytest.raises(pytest.UsageError):
        pytest_collection_modifyitems(_StubConfig("bogus"), [item])


@pytest.mark.timeout(120)  # raise the 60s global cap: this spawns a nested pytest
def test_bare_pytest_skips_converted_live_file_despite_env():
    """AC-1 (accident scenario): with IMP_LIVE_PROVIDERS exported, a bare pytest
    run (no --run-live) skips every live test in a converted module, citing the
    run-live reason. The ambient env is powerless against the flag gate."""
    env = {**os.environ, "IMP_LIVE_PROVIDERS": "anthropic,openai,claude_code,codex"}
    env["PYTHONPATH"] = (
        str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:randomly", "-rs", "-q",
            "tests/test_codex_provider_live.py",
        ],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=110,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "live-API test — pass --run-live to run" in combined
    assert "IMP_LIVE_PROVIDERS" not in combined  # old skip reason is gone


import json as _json  # noqa: E402

from tests._live import sandbox_sleep_precondition_failed  # noqa: E402

# The imp sandbox's per-command COW overlay home failing to mount:
_OVERLAY_STDERR = (
    "mount: /tmp/imp-abc123/w.overlay-merged: wrong fs type, bad option, "
    "bad superblock on overlay, missing codepage or helper program, "
    "or other error"
)

# Exact AC text (spec §"Acceptance criteria"); the "…" is U+2026.
_EXPECTED_REASON = (
    'imp bash-sandbox overlay mount failed (rc 32: "wrong fs type … on '
    'overlay"); the writer\'s `sleep 30` never ran, so the '
    "mid-turn-interrupt precondition is unmet. This is a sandbox/environment "
    "failure, not an interrupt-resume regression."
)


def _write_events(path, records, *, extra_lines=()):
    """Write each record dict as one JSON line; append any raw extra_lines
    (blank/garbage) verbatim to exercise tolerant parsing."""
    lines = [_json.dumps(rec) for rec in records]
    lines.extend(extra_lines)
    path.write_text("\n".join(lines) + "\n")


def _bash_call(tool_id="b1"):
    return {"type": "tool_call",
            "payload": {"id": tool_id, "name": "bash",
                        "input": {"command": "sleep 30"}}}


def _bash_result(tool_id="b1", *, returncode, stderr, stdout="", shape="str"):
    # shape="str": the anthropic/openai in-process runner stores the bash JSON
    # verbatim as a string. shape="list": claude_code's SDK wraps it as a list
    # of {"type":"text","text": <json string>} parts (see the real fixture
    # tests/fixtures/cc_tool_counter_burst.jsonl).
    inner = _json.dumps(
        {"stdout": stdout, "stderr": stderr, "returncode": returncode})
    content = inner if shape == "str" else [{"type": "text", "text": inner}]
    return {"type": "tool_result",
            "payload": {"id": tool_id, "content": content, "error": returncode != 0}}


def test_precondition_failed_returns_exact_reason_on_overlay_mount(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=32, stderr=_OVERLAY_STDERR),
    ])
    assert sandbox_sleep_precondition_failed(ev) == _EXPECTED_REASON


def test_precondition_failed_returns_reason_on_claude_code_list_content(tmp_path):
    # claude_code's bash tool_result content is a LIST of text parts, not a
    # string; the mount failure must still be detected so the two claude_code
    # targets SKIP (spec AC: all five tests SKIP on the overlay mount failure).
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=32, stderr=_OVERLAY_STDERR, shape="list"),
    ])
    assert sandbox_sleep_precondition_failed(ev) == _EXPECTED_REASON


def test_precondition_returns_none_on_clean_claude_code_list_content(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=0, stderr="", stdout="done", shape="list"),
    ])
    assert sandbox_sleep_precondition_failed(ev) is None


def test_precondition_returns_none_on_clean_bash(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=0, stderr="", stdout="done"),
    ])
    assert sandbox_sleep_precondition_failed(ev) is None


def test_precondition_returns_none_on_rc32_without_overlay_signature(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=32, stderr="mount: some unrelated error"),
    ])
    assert sandbox_sleep_precondition_failed(ev) is None


def test_precondition_returns_none_for_non_bash_tool(tmp_path):
    ev = tmp_path / "events.jsonl"
    read_call = {"type": "tool_call",
                 "payload": {"id": "r1", "name": "read_file",
                             "input": {"path": "a.txt"}}}
    # A rc-32 overlay-looking result on a NON-bash tool id must not match.
    _write_events(ev, [read_call, _bash_result("r1", returncode=32,
                                               stderr=_OVERLAY_STDERR)])
    assert sandbox_sleep_precondition_failed(ev) is None


def test_precondition_returns_none_when_events_missing(tmp_path):
    assert sandbox_sleep_precondition_failed(tmp_path / "nope.jsonl") is None


def test_precondition_tolerates_blank_and_garbage_lines(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(
        ev,
        [_bash_call(), _bash_result(returncode=32, stderr=_OVERLAY_STDERR)],
        extra_lines=("", "   ", "{not valid json", "partial-line-no-brace"),
    )
    assert sandbox_sleep_precondition_failed(ev) == _EXPECTED_REASON


def test_precondition_detects_mcp_qualified_bash_name(tmp_path):
    ev = tmp_path / "events.jsonl"
    mcp_bash_call = {"type": "tool_call",
                     "payload": {"id": "b1", "name": "mcp__imp__bash",
                                 "input": {"command": "sleep 30"}}}
    _write_events(ev, [
        mcp_bash_call,
        _bash_result(returncode=32, stderr=_OVERLAY_STDERR),
    ])
    assert sandbox_sleep_precondition_failed(ev) == _EXPECTED_REASON


from tests.test_relay_hibernation_live import _interrupt_gate_engaged  # noqa: E402


def _awaiting(interrupt):
    return {
        "type": "awaiting_user_input",
        "payload": {
            "steer_hint": "interrupted · enter to continue · type to redirect · Ctrl-D to leave",
            "interrupt": interrupt,
        },
    }


def test_interrupt_gate_engaged_true_on_interrupt_event(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=0, stderr="", stdout="done"),
        _awaiting(True),
    ])
    assert _interrupt_gate_engaged(ev) is True


def test_interrupt_gate_engaged_false_on_noninterrupt_yield(tmp_path):
    # A bare-turn-end yield ALSO emits awaiting_user_input, but with
    # interrupt=False; it must NOT be read as the cooperative-interrupt steer
    # gate. This is the falsifiable case: a type-only check would wrongly pass.
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [_awaiting(False)])
    assert _interrupt_gate_engaged(ev) is False


def test_interrupt_gate_engaged_false_when_no_awaiting_event(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        _bash_call(),
        _bash_result(returncode=0, stderr="", stdout="done"),
    ])
    assert _interrupt_gate_engaged(ev) is False


def test_interrupt_gate_engaged_false_when_events_missing(tmp_path):
    assert _interrupt_gate_engaged(tmp_path / "nope.jsonl") is False


def test_interrupt_gate_engaged_tolerates_blank_and_garbage_lines(tmp_path):
    ev = tmp_path / "events.jsonl"
    _write_events(
        ev,
        [_awaiting(True)],
        extra_lines=("", "   ", "{not valid json", "no-brace-line"),
    )
    assert _interrupt_gate_engaged(ev) is True


def test_impty_e2e_timeout_exceeds_global_cap():
    # The global ini timeout is 60s (signal). The impty e2e ceiling MUST exceed
    # it, else the marker override is a no-op against the SIGALRM cap.
    from tests.conftest import _IMPTY_E2E_TIMEOUT
    assert _IMPTY_E2E_TIMEOUT >= 180


def test_hook_adds_raised_timeout_to_impty_e2e_file():
    from tests.conftest import _IMPTY_E2E_TIMEOUT, pytest_collection_modifyitems
    item = _StubItem("tests/impty/test_e2e_pipeline.py")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    timeouts = [d for d in item.added if d.mark.name == "timeout"]
    assert timeouts and timeouts[0].mark.args[0] == _IMPTY_E2E_TIMEOUT
    assert timeouts[0].mark.args[0] > 60  # overrides the global SIGALRM cap


def test_hook_ignores_non_e2e_impty_file():
    # A non-e2e impty test (e.g. the session unit tests) keeps the global 60s
    # cap — the raised ceiling is scoped to the full-pipeline e2e files only.
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/impty/test_session.py")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    assert [d for d in item.added if d.mark.name == "timeout"] == []


def test_hook_does_not_add_timeout_to_ordinary_top_level_file():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_ordinary.py")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    assert [d for d in item.added if d.mark.name == "timeout"] == []


def test_e2e_poll_timeout_default_is_60():
    from tests.impty.e2e_support import E2E_POLL_TIMEOUT
    assert E2E_POLL_TIMEOUT == 60.0


def test_render_poll_timeout_is_knob_derived_double():
    from tests.impty.e2e_support import E2E_POLL_TIMEOUT, E2E_RENDER_POLL_TIMEOUT
    assert E2E_RENDER_POLL_TIMEOUT == E2E_POLL_TIMEOUT * 2
    assert E2E_RENDER_POLL_TIMEOUT > E2E_POLL_TIMEOUT
    assert E2E_RENDER_POLL_TIMEOUT == 120.0  # base default 60.0 doubled


def test_e2e_poll_timeout_overridable_via_env():
    env = {**os.environ, "IMP_E2E_POLL_TIMEOUT": "90"}
    env["PYTHONPATH"] = (
        str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, "-c",
         "from tests.impty.e2e_support import E2E_POLL_TIMEOUT; "
         "print(E2E_POLL_TIMEOUT)"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "90.0"


def test_shared_poll_helpers_defined_once_in_e2e_support():
    import tests.impty.e2e_support as sup
    import tests.test_relay_hibernation_live as live
    assert live._poll is sup._poll
    assert live._flock_free is sup._flock_free
    assert live._event_types is sup._event_types


def test_e2e_support_event_types_parses_types_in_order(tmp_path):
    from tests.impty.e2e_support import _event_types
    ev = tmp_path / "events.jsonl"
    ev.write_text(
        '{"type": "a"}\n\n{"type": "b"}\nnot-json\n{"no_type": 1}\n{"type": "c"}\n'
    )
    assert _event_types(ev) == ["a", "b", "c"]


def test_renamed_gate_file_inherits_impty_e2e_timeout():
    from tests.conftest import _IMPTY_E2E_TIMEOUT, pytest_collection_modifyitems
    item = _StubItem("tests/impty/test_e2e_relay_hibernation_gate.py")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    timeouts = [d for d in item.added if d.mark.name == "timeout"]
    assert timeouts and timeouts[0].mark.args[0] == _IMPTY_E2E_TIMEOUT


def test_attach_poll_seq_derived_from_poll_timeout():
    from tests.impty.e2e_support import E2E_POLL_TIMEOUT
    from tests.impty.test_e2e_relay_hibernation_gate import _ATTACH_POLL
    assert f"seq {int(E2E_POLL_TIMEOUT / 0.1)}" in _ATTACH_POLL
    assert "seq 200" not in _ATTACH_POLL


def test_gate_file_has_no_sub_knob_poll_literals():
    import tests.impty.test_e2e_relay_hibernation_gate as gate
    src = Path(gate.__file__).read_text()
    for lit in ("timeout=10.0", "timeout=30.0", "timeout=60.0"):
        assert lit not in src
    assert "E2E_POLL_TIMEOUT" in src


def test_live_sibling_routes_inner_polls_through_knob():
    import tests.test_relay_hibernation_live as live
    src = Path(live.__file__).read_text()
    assert "timeout=E2E_POLL_TIMEOUT" in src   # inner polls adopt the knob
    assert "timeout=E2E_RENDER_POLL_TIMEOUT" in src  # render poll gets the generous budget
    assert "timeout=60.0" not in src           # no bare 60s inner poll survives
    assert "timeout=120.0" in src              # real-provider round-trip stays explicit
    assert "timeout=30.0" in src              # _await_change_dir setup poll stays explicit


def test_interrupt_e2e_routes_gate_budget_through_knob():
    import tests.impty.test_e2e_interrupt as it
    src = Path(it.__file__).read_text()
    assert "E2E_POLL_TIMEOUT" in src            # the knob is adopted
    assert "+ 60.0" not in src                  # monotonic gate deadline no longer bare
    assert "hard=60.0" not in src               # run_to_completion routed through knob
    assert "hard=min(0.5, remaining)" in src    # inner clamp unchanged
