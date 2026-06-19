from __future__ import annotations

import tests.impty.test_e2e_lifecycle as e2e


def test_lifecycle_test_carries_180s_timeout_override() -> None:
    target = e2e.test_tui_lifecycle_renders_real_engine_tool_calls
    marks = [m for m in getattr(target, "pytestmark", []) if m.name == "timeout"]
    assert marks, "target test must carry a per-test @pytest.mark.timeout override"
    assert marks[0].args == (180,), f"expected a 180s timeout override, got {marks[0].args}"
