"""The shipped default workflow must pass loader validation as-is."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.errors import LoadErrors
from imp.engine.executor import RunResult, run_change
from imp.engine.loader import load_workflow
from imp.engine.state import State
from imp.providers import resolve
from imp.providers.anthropic import AnthropicProvider
from tests.anthropic_fakes import FakeAsyncAnthropic
from tests.test_default_workflow_e2e_mocked import (
    _canned_anthropic,
    _seed_default_workflow,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _register_fake_anthropic(fake: FakeAsyncAnthropic) -> None:
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )


def _started_steps(change) -> list[str]:
    return [
        e["payload"].get("step")
        for e in change.event_log().read()
        if e.get("type") == "step_started"
    ]


def _event_types(change) -> list[str]:
    return [e.get("type") for e in change.event_log().read()]


def test_bundled_workflow_dir_exists() -> None:
    wf = bundled_workflow_dir("imp")
    assert (wf / "workflow.yaml").is_file()
    assert (wf / "agents").is_dir()
    assert (wf / "scripts").is_dir()


def test_default_workflow_loads_with_default_roles(tmp_path: Path) -> None:
    # Stage the workflow into a tmp dir to ensure no .imp/ ambient leakage
    staged = tmp_path / "wf"
    shutil.copytree(bundled_workflow_dir("imp"), staged)
    # Tiers referenced by the default workflow
    configured = {"low", "medium", "high"}
    workflow = load_workflow(staged, configured_tiers=configured)
    assert workflow.name == "feature"
    assert [p.name for p in workflow.phases] == [
        "spec", "plan", "implement", "review", "merge",
    ]


def test_default_workflow_fails_with_missing_role(tmp_path: Path) -> None:
    staged = tmp_path / "wf"
    shutil.copytree(bundled_workflow_dir("imp"), staged)
    with pytest.raises(LoadErrors) as exc:
        load_workflow(staged, configured_tiers=frozenset({"medium"}))
    msg = str(exc.value)
    assert "low" in msg or "high" in msg  # at least one undefined tier surfaces


@pytest.mark.anyio
async def test_approved_clean_reintegration_merges_without_second_finalize(
    tmp_path,
) -> None:
    """Acceptance #1: after finalize approves and base advances with a CLEAN
    rebase, the change merges with NO second human prompt. base-fresh-check
    reports advanced once (exit 10) then unchanged (exit 0); the post-approval
    clean re-integration auto-proceeds to merge-to-base instead of routing back
    to finalize."""
    change = _seed_default_workflow(tmp_path)
    scripts = (change.project_root / "imp" / "workflows" / "imp" / "scripts")

    bfc = scripts / "base-fresh-check.sh"
    bfc.write_text(
        "#!/usr/bin/env bash\n"
        'f="${IMP_PROJECT_ROOT}/.imp/change/_bfc_calls"\n'
        'n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"\n'
        '[ "$n" -eq 1 ] && exit 10 || exit 0\n')
    bfc.chmod(0o755)
    # integrate-rebase: clean re-integration (the seeded stub is already exit 0;
    # made explicit for clarity).
    ir = scripts / "integrate-rebase.sh"
    ir.write_text("#!/usr/bin/env bash\nexit 0\n")
    ir.chmod(0o755)

    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic())
    _register_fake_anthropic(fake)

    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED

    started = _started_steps(change)
    # The human gate ran exactly once: the post-approval clean re-integration
    # auto-proceeded instead of re-soliciting.
    assert started.count("finalize") == 1, started
    # The re-integration actually happened (base advanced once -> 2 rebases:
    # the pre-approval pass + the post-approval re-integration).
    assert started.count("integrate-rebase") >= 2, started
    assert "merge-to-base" in started, started
    assert "change_merged" in _event_types(change)


@pytest.mark.anyio
async def test_approved_nothing_to_integrate_merges_without_second_finalize(
    tmp_path,
) -> None:
    """Edge case: after finalize approves and base advanced but integrate-rebase
    finds nothing to integrate (exit 2, base became an ancestor), the change
    routes to base-fresh-check and merges — no second human prompt."""
    change = _seed_default_workflow(tmp_path)
    scripts = (change.project_root / "imp" / "workflows" / "imp" / "scripts")

    bfc = scripts / "base-fresh-check.sh"
    bfc.write_text(
        "#!/usr/bin/env bash\n"
        'f="${IMP_PROJECT_ROOT}/.imp/change/_bfc_calls"\n'
        'n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"\n'
        '[ "$n" -eq 1 ] && exit 10 || exit 0\n')
    bfc.chmod(0o755)
    ir = scripts / "integrate-rebase.sh"
    ir.write_text("#!/usr/bin/env bash\nexit 2\n")
    ir.chmod(0o755)

    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic())
    _register_fake_anthropic(fake)

    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED

    started = _started_steps(change)
    assert started.count("finalize") == 1, started
    assert "merge-to-base" in started, started
    assert "change_merged" in _event_types(change)


@pytest.mark.anyio
async def test_approved_conflict_reintegration_routes_back_to_finalize(
    tmp_path,
) -> None:
    """Acceptance #2: after finalize approves and re-integration hits a CONFLICT
    (integrate-rebase exit 6), the change routes back to finalize for human
    review — one re-ask per conflict round. integrate-rebase is clean on the
    pre-approval pass (call 1) and conflicts on the re-integration (call 2)."""
    change = _seed_default_workflow(tmp_path)
    scripts = (change.project_root / "imp" / "workflows" / "imp" / "scripts")

    bfc = scripts / "base-fresh-check.sh"
    bfc.write_text(
        "#!/usr/bin/env bash\n"
        'f="${IMP_PROJECT_ROOT}/.imp/change/_bfc_calls"\n'
        'n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"\n'
        '[ "$n" -eq 1 ] && exit 10 || exit 0\n')
    bfc.chmod(0o755)
    ir = scripts / "integrate-rebase.sh"
    ir.write_text(
        "#!/usr/bin/env bash\n"
        'f="${IMP_PROJECT_ROOT}/.imp/change/_ir_calls"\n'
        'n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"\n'
        '[ "$n" -eq 1 ] && exit 0 || exit 6\n')
    ir.chmod(0o755)

    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic())
    _register_fake_anthropic(fake)

    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED

    started = _started_steps(change)
    # finalize ran twice: the initial approval + the conflict re-ask.
    assert started.count("finalize") == 2, started
    # The conflict routed through the integrator.
    assert "integrator" in started, started
    assert "change_merged" in _event_types(change)


@pytest.mark.parametrize(
    ("rebase_exit", "expect_integrator"), [(0, False), (6, True)],
)
@pytest.mark.anyio
async def test_preapproval_first_pass_reaches_finalize(
    tmp_path, rebase_exit, expect_integrator,
) -> None:
    """Acceptance #3: pre-approval routing is unchanged. With merge_approved
    still false, the first pass lands on finalize for BOTH a clean rebase
    (exit 0 -> integrate-verify -> finalize) and a conflict rebase (exit 6 ->
    integrator -> integrate-verify -> finalize). No base drift here
    (base-fresh-check stays exit 0), so finalize runs exactly once."""
    change = _seed_default_workflow(tmp_path)
    scripts = (change.project_root / "imp" / "workflows" / "imp" / "scripts")
    ir = scripts / "integrate-rebase.sh"
    ir.write_text(f"#!/usr/bin/env bash\nexit {rebase_exit}\n")
    ir.chmod(0o755)

    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic())
    _register_fake_anthropic(fake)

    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED

    started = _started_steps(change)
    assert started.count("finalize") == 1, started
    assert ("integrator" in started) is expect_integrator, started
    assert "change_merged" in _event_types(change)


@pytest.mark.anyio
async def test_post_approval_clean_loop_is_bounded(tmp_path) -> None:
    """Acceptance #4: the post-approval clean loop is bounded by
    max_loops: 10 on base-fresh-check. base-fresh-check always reports advanced
    (exit 10) and integrate-rebase always rebases clean (exit 0), so the machine
    loops base-fresh-check -> integrate-rebase -> integrate-verify ->
    base-fresh-check. After 11 consecutive base-fresh-check exit-10 rounds the
    11th backward goto trips the bound (n > 10) and the engine pauses with
    reason max_loops rather than looping forever."""
    change = _seed_default_workflow(tmp_path)
    scripts = (change.project_root / "imp" / "workflows" / "imp" / "scripts")
    bfc = scripts / "base-fresh-check.sh"
    bfc.write_text("#!/usr/bin/env bash\nexit 10\n")
    bfc.chmod(0o755)
    ir = scripts / "integrate-rebase.sh"
    ir.write_text("#!/usr/bin/env bash\nexit 0\n")
    ir.chmod(0o755)

    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic())
    _register_fake_anthropic(fake)

    result = await run_change(change, non_interactive=True)
    assert result is RunResult.PAUSED

    state = State.load(change.state_dir)
    assert state.status == "paused"
    paused = [
        e for e in change.event_log().read()
        if e.get("type") == "workflow_paused"
    ]
    assert paused, "expected a workflow_paused event"
    assert paused[-1]["payload"]["reason"] == "max_loops", paused[-1]

    started = _started_steps(change)
    # 10 rounds are allowed; the 11th run computes the goto that trips the bound.
    assert started.count("base-fresh-check") == 11, started.count("base-fresh-check")
