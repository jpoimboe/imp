import json
import stat
from pathlib import Path

from imp.providers.codex_config import teardown_codex_home


def _seed(home: Path, *, with_auth: bool = True) -> None:
    home.mkdir(mode=0o700, parents=True)
    if with_auth:
        body = {"auth_mode": "chatgpt",
                "tokens": {"refresh_token": "rt", "access_token": "at",
                           "last_refresh": "2026-06-01T00:00:00Z"}}
        (home / "auth.json").write_text(json.dumps(body))
        (home / "auth.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
    (home / "config.toml").write_text("[mcp_servers.imp]\n")


def test_teardown_removes_directory_on_apikey_mode(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    _seed(home)
    teardown_codex_home(
        codex_home=home, mode="apikey", canonical_path=tmp_path / "canon")
    assert not home.exists()


def test_teardown_chatgpt_shreds_auth_and_leaves_canonical_untouched(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.json"
    canonical.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"refresh_token": "rt", "access_token": "at-canon",
                   "last_refresh": "2026-06-01T00:00:00Z"}}))
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    # A refreshed CODEX_HOME copy (newer last_refresh) — under the old
    # propagate-back this would overwrite canonical; now it must NOT.
    body = {"auth_mode": "chatgpt",
            "tokens": {"refresh_token": "rt", "access_token": "at-home",
                       "last_refresh": "2026-06-01T05:00:00Z"}}
    (home / "auth.json").write_text(json.dumps(body))
    (home / "auth.json").chmod(stat.S_IRUSR | stat.S_IWUSR)

    teardown_codex_home(
        codex_home=home, mode="chatgpt", canonical_path=canonical)

    # No propagate-back: teardown never mutates canonical.
    assert json.loads(canonical.read_text())["tokens"]["access_token"] == "at-canon"
    # The throwaway CODEX_HOME credential and dir are gone.
    assert not home.exists()


def test_teardown_is_idempotent_when_nothing_exists(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"  # never created
    # Must not raise even though neither the dir nor auth.json exists.
    teardown_codex_home(
        codex_home=home, mode="chatgpt", canonical_path=tmp_path / "canon")
    assert not home.exists()
