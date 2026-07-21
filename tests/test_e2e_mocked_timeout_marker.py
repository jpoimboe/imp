from __future__ import annotations


def test_mocked_e2e_module_carries_300s_timeout_marker() -> None:
    # Full multi-phase workflow run (2-5s normal); 300s is hang-catching headroom under -n auto.
    import tests.test_default_workflow_e2e_mocked as e2e

    marks = getattr(e2e, "pytestmark", [])
    timeouts = [m for m in marks if getattr(m, "name", None) == "timeout"]
    assert timeouts, "e2e module must carry a pytest.mark.timeout marker"
    assert timeouts[0].args == (300,), (
        f"expected a 300s timeout, got {timeouts[0].args}"
    )
