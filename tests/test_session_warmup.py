from __future__ import annotations

import subprocess
import sys
import textwrap


def test_warmup_imports_full_graph_with_zero_failures() -> None:
    # Fresh interpreter: a real import-safety guard; the representative-leaf
    # check keeps a shrunken WARMUP_PACKAGES from passing trivially.
    code = textwrap.dedent(
        """
        import sys
        from imp.remote.session.warmup import warm_module_graph

        failures = warm_module_graph()
        for name, exc in failures:
            print(f"FAIL {name}: {exc!r}")
        print(f"COUNT {len(failures)}")

        reps = [
            "imp.engine.executor",
            "imp.providers.codex_runner",
            "imp.agents.skills",
            "imp.remote.session.daemon",
            "imp.cli.main",
        ]
        missing = [r for r in reps if r not in sys.modules]
        print(f"MISSING {missing}")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "COUNT 0" in out.stdout, (
        "warmup hit import failures:\n" + out.stdout + out.stderr
    )
    assert "MISSING []" in out.stdout, (
        "warmup did not cover every subpackage:\n" + out.stdout + out.stderr
    )


def test_main_warms_module_graph_before_serving(monkeypatch, tmp_path) -> None:
    # Warmup must run unconditionally, before anyio.run drives the quest.
    import imp.remote.session.daemon as daemon

    calls: list[str] = []

    def _fake_warm() -> list[tuple[str, BaseException]]:
        calls.append("warmup")
        return []

    def _fake_run(*args, **kwargs) -> int:
        calls.append("run")
        return 0

    monkeypatch.setattr(daemon, "warm_module_graph", _fake_warm)
    def _fake_load(cls, root, qid):
        calls.append("load")
        return object()

    monkeypatch.setattr(daemon.Quest, "load", classmethod(_fake_load))
    monkeypatch.setattr(daemon.anyio, "run", _fake_run)

    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "abc123"])

    assert calls == ["warmup", "load", "run"]
    assert rc == 0


def test_completion_path_modules_are_cached_after_warmup() -> None:
    # After warmup, completion-path modules are already in sys.modules, so a
    # later first-time reference is a cache hit, not a fresh disk import.
    import importlib

    from imp.remote.session.warmup import warm_module_graph

    warm_module_graph()

    targets = ["imp.providers.codex_runner", "imp.remote.session.client"]
    missing = [t for t in targets if t not in sys.modules]
    assert missing == [], f"warmup did not cache completion-path modules: {missing}"

    # The marker survives re-import only on a cache hit; a fresh disk import
    # would re-execute the module body and drop it.
    cached = sys.modules["imp.providers.codex_runner"]
    marker = object()
    cached.__dict__["_warmup_probe"] = marker
    reimported = importlib.import_module("imp.providers.codex_runner")
    assert reimported.__dict__.get("_warmup_probe") is marker
