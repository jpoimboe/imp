from imp.providers.codex_config import render_config_toml


def test_config_toml_emits_only_required_sections() -> None:
    txt = render_config_toml(host="127.0.0.1", port=49152,
                             bearer_env_var="IMP_MCP_TOKEN")
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
    # Nothing else (no sandbox_mode, no model, no web_search — those live in
    # SDK options to keep config precedence single-channel).
    assert "sandbox_mode" not in txt
    assert "web_search" not in txt
    assert "model = " not in txt
