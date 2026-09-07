"""Non-live unit test for the native-denial evidence helper in tests/_live.py.

Not `live`-marked: the live probe that consumes this helper cannot run in CI
(`--run-live codex` plus real credentials), so the verifiable coverage lives
here, driving the pure helper with synthetic report strings."""

from __future__ import annotations

from tests._live import native_denial_miss

_VERBATIM_PATCH = ("patch rejected: writing is blocked by read-only sandbox; "
                   "rejected by user approval settings")
_VERBATIM_EXEC = "TypeError: tools.exec_command is not a function"
_PARAPHRASED_EXEC = "the native exec_command tool was not available in this session."


def test_paraphrased_exec_denial_is_evidence() -> None:
    assert native_denial_miss(_VERBATIM_PATCH, _PARAPHRASED_EXEC) is None


def test_measured_verbatim_errors_are_evidence() -> None:
    assert native_denial_miss(_VERBATIM_PATCH, _VERBATIM_EXEC) is None


def test_success_placeholder_misses_and_names_its_field() -> None:
    miss = native_denial_miss("NO ERROR — IT SUCCEEDED", _VERBATIM_EXEC)
    assert miss is not None
    assert miss.startswith("patch_error=")
    assert "exec_error=" not in miss
    # The helper matches (and renders) the lowercased report.
    assert "no error — it succeeded" in miss


def test_empty_report_misses() -> None:
    assert native_denial_miss(_VERBATIM_PATCH, "") == (
        "exec_error='' carries no denial language (the model likely never "
        "attempted the native shell write)")


def test_never_attempted_veto_overrides_denial_markers() -> None:
    # Matches "sandbox" and "read-only", but "skipped" vetoes both.
    miss = native_denial_miss(
        "I skipped this step because the sandbox is read-only", _VERBATIM_EXEC)
    assert miss is not None
    assert miss.startswith("patch_error=")
    assert "exec_error=" not in miss
    # Same veto via the success-placeholder wording, which only bites when a
    # denial marker is also present to consult it.
    placeholder = native_denial_miss(
        "NO ERROR — IT SUCCEEDED; the sandbox is read-only for other tools",
        _VERBATIM_EXEC)
    assert placeholder is not None
    assert placeholder.startswith("patch_error=")
    assert "exec_error=" not in placeholder


def test_both_legs_missing_are_joined() -> None:
    assert native_denial_miss("", "I did not try it") == (
        "patch_error='' carries no denial language (the model likely never "
        "attempted the native apply_patch); "
        "exec_error='i did not try it' carries no denial language (the model "
        "likely never attempted the native shell write)")
