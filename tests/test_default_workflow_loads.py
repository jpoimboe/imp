from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow


def test_default_workflow_has_merge_var_and_no_artifacts_paths() -> None:
    wf = load_workflow(default_workflow_dir())
    assert any(v.name == "ready_to_merge" for v in wf.vars.decls)
    text = (default_workflow_dir() / "workflow.yaml").read_text()
    assert "/artifacts/" not in text


def test_no_agent_prompt_uses_artifacts_namespace() -> None:
    for md in (default_workflow_dir() / "agents").glob("*.md"):
        body = md.read_text()
        assert "{{ artifacts" not in body, md.name
        assert "/artifacts/" not in body, md.name
