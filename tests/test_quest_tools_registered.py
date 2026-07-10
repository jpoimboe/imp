def test_registry_exposes_quest_tools() -> None:
    from imp.agents.tools import get_registry
    reg = get_registry()
    assert "create_quest" in reg
    assert "rename_quest" in reg
    assert "create_" "change" not in reg  # split literal: dodges the rename sweep
    assert "rename_" "change" not in reg  # split literal: dodges the rename sweep
