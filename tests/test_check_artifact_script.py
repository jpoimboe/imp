"""check-artifact.sh was removed in the superpowers workflow port.

Artifact validation is now done by spec-commit-verify.sh and
plan-commit-verify.sh (see test_commit_verify_scripts.py for their tests).
This file verifies those replacement scripts are present and executable.
"""

from imp.assets import default_workflow_dir


def test_commit_verify_scripts_exist() -> None:
    scripts = default_workflow_dir() / "scripts"
    for name in ("spec-commit-verify.sh", "plan-commit-verify.sh"):
        p = scripts / name
        assert p.exists(), f"missing script: {p}"
        assert p.stat().st_mode & 0o111, f"script not executable: {p}"
