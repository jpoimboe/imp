from pathlib import Path

from imp.providers.codex_config import render_config_toml
from tests.conftest import _make_imp_checkout


def test_config_toml_emits_required_sections_and_disables_web_search() -> None:
    txt = render_config_toml(host="127.0.0.1", port=49152,
                             bearer_env_var="IMP_MCP_TOKEN",
                             web_search_enabled=False)
    # MCP server block — URL is loopback + ephemeral port; bearer-token env var
    # is the imp-side variable name (the value lives in env, not config).
    assert "[mcp_servers.imp]" in txt
    assert 'url = "http://127.0.0.1:49152/mcp"' in txt
    assert 'bearer_token_env_var = "IMP_MCP_TOKEN"' in txt
    # [features] block — disable everything disableable (spike round 5 Gate A).
    assert "[features]" in txt
    assert "shell_tool = false" in txt
    assert "plugins = false" in txt
    assert "image_generation = false" in txt
    # web_search MUST be controlled here (the deprecated SDK flag no longer
    # disables on codex >=0.137). Default agent -> disabled.
    assert 'web_search = "disabled"' in txt
    # It must be a top-level key (precede every [table] in TOML), else it's
    # silently parsed into the preceding table.
    assert txt.index('web_search = "disabled"') < txt.index("[mcp_servers.imp]")
    # Still nothing else leaking from the SDK-options channel.
    assert "sandbox_mode" not in txt
    assert "model = " not in txt


def test_config_toml_enables_web_search_when_declared() -> None:
    txt = render_config_toml(host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=True)
    assert 'web_search = "live"' in txt
    assert 'web_search = "disabled"' not in txt


def test_config_toml_mirrors_driver_as_stdio_block() -> None:
    txt = render_config_toml(host="127.0.0.1", port=49152,
                             bearer_env_var="IMP_MCP_TOKEN",
                             web_search_enabled=False, drivers=["impty"])
    assert "[mcp_servers.impty]" in txt
    assert "[mcp_servers.imp]" in txt
    block = txt[txt.index("[mcp_servers.impty]"):]
    assert 'command = "python"' in block
    assert 'args = ["-m", "impty.mcp"]' in block
    assert "url =" not in block
    assert "bearer_token_env_var =" not in block


def test_config_toml_no_driver_block_when_none() -> None:
    txt = render_config_toml(host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=False)
    assert "[mcp_servers.impty]" not in txt


def test_driver_block_gains_self_host_env_on_an_imp_checkout(tmp_path) -> None:
    _make_imp_checkout(tmp_path)
    txt = render_config_toml(host="127.0.0.1", port=1, bearer_env_var="X",
                             web_search_enabled=False, drivers=["impty"],
                             project_root=tmp_path)
    block = txt[txt.index("[mcp_servers.impty]"):]
    assert "env = {" in block
    assert f'PYTHONPATH = "{tmp_path / "src"}' in block
    assert f'PATH = "{tmp_path / ".imp" / "selfhost-bin"}' in block


def test_driver_block_has_no_env_for_a_non_imp_project(tmp_path) -> None:
    txt = render_config_toml(host="127.0.0.1", port=1, bearer_env_var="X",
                             web_search_enabled=False, drivers=["impty"],
                             project_root=tmp_path)
    block = txt[txt.index("[mcp_servers.impty]"):]
    assert "env = {" not in block
