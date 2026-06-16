
from imp.providers.codex_config import render_config_toml


def test_config_toml_emits_required_sections_and_disables_web_search() -> None:
    txt = render_config_toml(host="127.0.0.1", port=49152,
                             bearer_env_var="IMP_MCP_TOKEN",
                             web_search_enabled=False)
    assert "[mcp_servers.imp]" in txt
    assert 'url = "http://127.0.0.1:49152/mcp"' in txt
    assert 'bearer_token_env_var = "IMP_MCP_TOKEN"' in txt
    assert "[features]" in txt
    assert "shell_tool = false" in txt
    assert "plugins = false" in txt
    assert "image_generation = false" in txt
    assert 'web_search = "disabled"' in txt
    assert txt.index('web_search = "disabled"') < txt.index("[mcp_servers.imp]")
    assert "sandbox_mode" not in txt
    assert "model = " not in txt


def test_config_toml_enables_web_search_when_declared() -> None:
    txt = render_config_toml(host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=True)
    assert 'web_search = "live"' in txt
    assert 'web_search = "disabled"' not in txt


def test_config_toml_emits_impty_as_http_server_block() -> None:
    txt = render_config_toml(
        host="127.0.0.1", port=49152, bearer_env_var="IMP_MCP_TOKEN",
        web_search_enabled=False, impty_url="http://127.0.0.1:55001/mcp",
        impty_bearer_env_var="IMP_IMPTY_TOKEN")
    assert "[mcp_servers.impty]" in txt
    assert "[mcp_servers.imp]" in txt
    block = txt[txt.index("[mcp_servers.impty]"):]
    # In-process HTTP server: a url + bearer env var, NOT a stdio command/args.
    assert 'url = "http://127.0.0.1:55001/mcp"' in block
    assert 'bearer_token_env_var = "IMP_IMPTY_TOKEN"' in block
    assert "command =" not in block
    assert "args =" not in block


def test_config_toml_no_impty_block_when_not_active() -> None:
    txt = render_config_toml(host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=False)
    assert "[mcp_servers.impty]" not in txt
