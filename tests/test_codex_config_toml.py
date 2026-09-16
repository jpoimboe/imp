import tomllib

import pytest

from imp.providers.codex_config import render_config_toml

# The full production renderings, pinned byte-for-byte. Codex's config layer
# fails open: an unknown key inside [features] or [mcp_servers.*] is ignored with
# exit 0 and no warning, so a typo in a hardening flag is invisible at runtime.
_EXPECTED_IMP_ONLY = '''web_search = "disabled"
model_auto_compact_token_limit = 100000000

[mcp_servers.imp]
url = "http://127.0.0.1:41234/mcp"
bearer_token_env_var = "IMP_MCP_BEARER_TOKEN"
tool_timeout_sec = 86400
default_tools_approval_mode = "approve"

[features]
apps = false
goals = false
image_generation = false
multi_agent = false
plugins = false
shell_tool = false
sleep_tool = false
tool_suggest = false
view_image = false

[features.code_mode]
direct_only_tool_namespaces = ["mcp__imp"]
'''

_EXPECTED_WITH_IMPTY = '''web_search = "disabled"
model_auto_compact_token_limit = 100000000

[mcp_servers.imp]
url = "http://127.0.0.1:41234/mcp"
bearer_token_env_var = "IMP_MCP_BEARER_TOKEN"
tool_timeout_sec = 86400
default_tools_approval_mode = "approve"

[features]
apps = false
goals = false
image_generation = false
multi_agent = false
plugins = false
shell_tool = false
sleep_tool = false
tool_suggest = false
view_image = false

[features.code_mode]
direct_only_tool_namespaces = ["mcp__imp", "mcp__impty"]

[mcp_servers.impty]
url = "http://127.0.0.1:55001/mcp"
bearer_token_env_var = "IMP_IMPTY_BEARER_TOKEN"
tool_timeout_sec = 86400
default_tools_approval_mode = "approve"
'''


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


def test_config_toml_pins_the_per_call_tool_timeout_in_both_server_blocks() -> None:
    # Codex applies tool_timeout_sec per MCP server as a plain wall clock and
    # defaults to 300s, which would cut off any longer imp or impty tool call.
    txt = render_config_toml(
        host="127.0.0.1", port=49152, bearer_env_var="IMP_MCP_TOKEN",
        web_search_enabled=False, impty_url="http://127.0.0.1:55001/mcp",
        impty_bearer_env_var="IMP_IMPTY_TOKEN")
    cfg = tomllib.loads(txt)
    assert cfg["mcp_servers"]["imp"]["tool_timeout_sec"] == 86400
    assert cfg["mcp_servers"]["impty"]["tool_timeout_sec"] == 86400


def test_config_toml_pins_the_imp_tool_timeout_without_an_impty_block() -> None:
    txt = render_config_toml(host="127.0.0.1", port=1,
                             bearer_env_var="X", web_search_enabled=False)
    cfg = tomllib.loads(txt)
    assert cfg["mcp_servers"]["imp"]["tool_timeout_sec"] == 86400
    assert "impty" not in cfg["mcp_servers"]


def test_config_toml_puts_approval_mode_inside_the_imp_server_table() -> None:
    txt = render_config_toml(host="127.0.0.1", port=49152,
                             bearer_env_var="IMP_MCP_TOKEN",
                             web_search_enabled=False)
    data = tomllib.loads(txt)
    # Parsed, not substring-matched: a key landing in the wrong table must fail.
    assert data["mcp_servers"]["imp"]["default_tools_approval_mode"] == "approve"
    assert "default_tools_approval_mode" not in data["features"]
    # All ten parsed entries: the nine hardening booleans plus the nested
    # code_mode sub-table. Exact equality, so a stray key fails here.
    assert data["features"] == {
        "apps": False,
        "goals": False,
        "image_generation": False,
        "multi_agent": False,
        "plugins": False,
        "shell_tool": False,
        "sleep_tool": False,
        "tool_suggest": False,
        "view_image": False,
        "code_mode": {"direct_only_tool_namespaces": ["mcp__imp"]},
    }


def test_config_toml_puts_approval_mode_inside_the_impty_server_table() -> None:
    txt = render_config_toml(
        host="127.0.0.1", port=49152, bearer_env_var="IMP_MCP_TOKEN",
        web_search_enabled=False, impty_url="http://127.0.0.1:55001/mcp",
        impty_bearer_env_var="IMP_IMPTY_TOKEN")
    data = tomllib.loads(txt)
    assert data["mcp_servers"]["impty"]["default_tools_approval_mode"] == "approve"
    assert data["mcp_servers"]["impty"]["url"] == "http://127.0.0.1:55001/mcp"
    assert data["mcp_servers"]["imp"]["default_tools_approval_mode"] == "approve"
    assert data["features"]["shell_tool"] is False


@pytest.mark.parametrize("web_search_enabled", [False, True])
@pytest.mark.parametrize(
    ("impty_url", "impty_bearer_env_var"),
    [(None, None), ("http://127.0.0.1:55001/mcp", "IMP_IMPTY_TOKEN")],
)
def test_config_toml_disables_codex_auto_compaction_in_every_variant(
    web_search_enabled: bool,
    impty_url: str | None,
    impty_bearer_env_var: str | None,
) -> None:
    txt = render_config_toml(
        host="127.0.0.1", port=49152, bearer_env_var="IMP_MCP_TOKEN",
        web_search_enabled=web_search_enabled, impty_url=impty_url,
        impty_bearer_env_var=impty_bearer_env_var)
    cfg = tomllib.loads(txt)
    # Codex reads <= 0 as "compact on every request" (measured), so the only
    # verified disable is a limit above any real window.
    assert cfg["model_auto_compact_token_limit"] == 100000000
    # The scope key is irrelevant with the limit unreachable, and an invalid
    # value there is a hard config-load failure.
    assert "model_auto_compact_token_limit_scope" not in txt


def test_config_toml_emits_auto_compact_limit_before_the_first_table() -> None:
    txt = render_config_toml(host="127.0.0.1", port=49152,
                             bearer_env_var="IMP_MCP_TOKEN",
                             web_search_enabled=False)
    # TOML: a top-level key after the first [table] header lands inside that
    # table instead, so the ordering is load-bearing, not cosmetic.
    assert txt.index("model_auto_compact_token_limit") < txt.index("[")
    assert txt.index('web_search = "disabled"') < txt.index("[mcp_servers.imp]")


def test_config_toml_snapshot_pins_the_imp_only_rendering() -> None:
    txt = render_config_toml(host="127.0.0.1", port=41234,
                             bearer_env_var="IMP_MCP_BEARER_TOKEN",
                             web_search_enabled=False)
    assert txt == _EXPECTED_IMP_ONLY


def test_config_toml_snapshot_pins_the_impty_held_rendering() -> None:
    txt = render_config_toml(
        host="127.0.0.1", port=41234, bearer_env_var="IMP_MCP_BEARER_TOKEN",
        web_search_enabled=False, impty_url="http://127.0.0.1:55001/mcp",
        impty_bearer_env_var="IMP_IMPTY_BEARER_TOKEN")
    assert txt == _EXPECTED_WITH_IMPTY


def test_direct_only_namespaces_track_the_servers_actually_configured() -> None:
    # Without impty listed, impty's tools would stay behind codex's JavaScript
    # `exec` indirection while imp's went direct.
    imp_only = tomllib.loads(render_config_toml(
        host="127.0.0.1", port=1, bearer_env_var="X", web_search_enabled=False))
    assert imp_only["features"]["code_mode"]["direct_only_tool_namespaces"] == ["mcp__imp"]
    with_impty = tomllib.loads(render_config_toml(
        host="127.0.0.1", port=1, bearer_env_var="X", web_search_enabled=False,
        impty_url="http://127.0.0.1:55001/mcp", impty_bearer_env_var="T"))
    assert with_impty["features"]["code_mode"]["direct_only_tool_namespaces"] == [
        "mcp__imp", "mcp__impty"]


def test_namespaces_carry_no_dangling_separator() -> None:
    # `mcp__imp__` is the namespace identifier plus a dangling `__` separator:
    # codex compares exact strings, matches nothing, and silently falls back to
    # code mode with no error and no warning (measured, spike cells C and D).
    imp_only = render_config_toml(host="127.0.0.1", port=1, bearer_env_var="X",
                                  web_search_enabled=False)
    with_impty = render_config_toml(
        host="127.0.0.1", port=1, bearer_env_var="X", web_search_enabled=False,
        impty_url="http://127.0.0.1:55001/mcp", impty_bearer_env_var="T")
    assert "mcp__imp__" not in imp_only
    assert "mcp__imp__" not in with_impty
