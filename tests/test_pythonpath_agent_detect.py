from __future__ import annotations

from pathlib import Path

from imp.agents.detached import (
    DetachedEnvironmentFailure,
    DetachedSuccess,
    DetachedTransientFailure,
)
from imp.assets import bundled_workflow_dir
from imp.cli.pythonpath_detect import (
    DetectedRoots,
    DetectionFailed,
    detect_pythonpath_via_agent,
)
from imp.engine.sandbox import SandboxConfig


def _run(tmp_path, monkeypatch, fake_result):
    async def _fake(**_kwargs):
        return fake_result
    import imp.cli.pythonpath_detect as pp
    monkeypatch.setattr(pp, "run_detached_agent", _fake)
    return detect_pythonpath_via_agent(
        tmp_path, bundled_workflow_dir("default"), "anthropic", "m",
        SandboxConfig(allow_unsandboxed=True))


def test_success_returns_validated_roots(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "src").mkdir()
    out = _run(tmp_path, monkeypatch, DetachedSuccess(
        {"status": "Success", "pythonpath": {"roots": ["src"]}}))
    assert out == DetectedRoots(["src"])


def test_empty_roots_is_valid_no_package(tmp_path: Path, monkeypatch) -> None:
    out = _run(tmp_path, monkeypatch, DetachedSuccess(
        {"status": "Success", "pythonpath": {"roots": []}}))
    assert out == DetectedRoots([])


def test_all_invalid_roots_is_transient_failure(tmp_path: Path, monkeypatch) -> None:
    # A non-empty answer that survives none of disk validation is surfaced, not
    # silently substituted.
    out = _run(tmp_path, monkeypatch, DetachedSuccess(
        {"status": "Success", "pythonpath": {"roots": ["nope", "/etc"]}}))
    assert isinstance(out, DetectionFailed)
    assert out.kind == "transient"


def test_error_status_is_transient_failure(tmp_path: Path, monkeypatch) -> None:
    out = _run(tmp_path, monkeypatch, DetachedSuccess(
        {"status": "Error", "pythonpath": {"roots": []}}))
    assert isinstance(out, DetectionFailed)
    assert out.kind == "transient"


def test_auth_failure_is_credential(tmp_path: Path, monkeypatch) -> None:
    out = _run(tmp_path, monkeypatch,
               DetachedEnvironmentFailure("auth", "bad key"))
    assert out == DetectionFailed("credential", "bad key")


def test_config_failure_is_credential(tmp_path: Path, monkeypatch) -> None:
    out = _run(tmp_path, monkeypatch,
               DetachedEnvironmentFailure("config", "rejected"))
    assert isinstance(out, DetectionFailed)
    assert out.kind == "credential"


def test_sandbox_env_failure_is_transient(tmp_path: Path, monkeypatch) -> None:
    out = _run(tmp_path, monkeypatch,
               DetachedEnvironmentFailure("sandbox", "no bwrap"))
    assert isinstance(out, DetectionFailed)
    assert out.kind == "transient"


def test_transient_primitive_failure_is_transient(tmp_path: Path, monkeypatch) -> None:
    out = _run(tmp_path, monkeypatch,
               DetachedTransientFailure("timeout", "slow"))
    assert isinstance(out, DetectionFailed)
    assert out.kind == "transient"
    assert out.detail == "timeout: slow"


def test_null_pythonpath_value_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    # Agent output boundary: `pythonpath` present but null must not crash.
    out = _run(tmp_path, monkeypatch, DetachedSuccess(
        {"status": "Success", "pythonpath": None}))
    assert out == DetectedRoots([])
