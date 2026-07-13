from __future__ import annotations


def test_mocked_e2e_module_carries_120s_timeout_marker() -> None:
    # The e2e suite sits near the global 60s per-test alarm; 120s gives 2x headroom.
    import tests.test_default_workflow_e2e_mocked as e2e

    marks = getattr(e2e, "pytestmark", [])
    timeouts = [m for m in marks if getattr(m, "name", None) == "timeout"]
    assert timeouts, "e2e module must carry a pytest.mark.timeout marker"
    assert timeouts[0].args == (120,), (
        f"expected a 120s timeout, got {timeouts[0].args}"
    )
