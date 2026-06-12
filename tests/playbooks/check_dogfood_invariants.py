#!/usr/bin/env python3
"""Post-run invariant checker for the dogfood playbook.

Standalone and provider-free: given a completed imp run's repo/worktree and the
unique IMP_DOGFOOD_<hex> sentinel the feature intent requested, assert the stable
mechanical invariants and print a machine-readable PASS/FAIL summary. Exit 0 iff
all pass. The run's commits are <base>..HEAD (default base: origin/HEAD), so every
check is location-agnostic — no pinned file, line, or commit count.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _fmt(paths: list[str]) -> str:
    return ", ".join(paths)


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=30,
    )


def check_clean_tree(repo: str) -> Result:
    dirty = _git(repo, "status", "--porcelain").stdout.strip()
    return Result("clean_working_tree", not dirty,
                  "clean" if not dirty else f"dirty:\n{dirty}")


def check_no_imp_in_history(repo: str, base: str) -> Result:
    out = _git(repo, "log", "--name-only", "--pretty=format:",
               f"{base}..HEAD").stdout
    imp_paths = sorted({ln for ln in out.splitlines()
                        if ln.startswith(".imp/")})
    return Result("no_imp_state_in_history", not imp_paths,
                  "none" if not imp_paths else f"committed: {_fmt(imp_paths)}")


def sentinel_src_files(repo: str, sentinel: str) -> list[str]:
    out = _git(repo, "grep", "-l", "-F", sentinel, "HEAD", "--", "src/").stdout
    return [ln.split(":", 1)[1] for ln in out.splitlines() if ":" in ln]


def check_sentinel_in_src(repo: str, sentinel: str) -> Result:
    files = sentinel_src_files(repo, sentinel)
    return Result("sentinel_in_committed_src", bool(files),
                  f"found in {_fmt(files)}" if files
                  else f"sentinel {sentinel!r} absent from committed src/")


def check_atomic_coder_commit(repo: str, base: str, sentinel: str) -> Result:
    files = sentinel_src_files(repo, sentinel)
    if not files:
        return Result("atomic_coder_commit", False,
                      "no sentinel-bearing src/ file to attribute")
    commits: set[str] = set()
    for f in files:
        out = _git(repo, "log", "--no-merges", "--format=%H",
                   f"{base}..HEAD", "--", f).stdout
        commits.update(h for h in out.splitlines() if h.strip())
    return Result("atomic_coder_commit", bool(commits),
                  f"{len(commits)} non-merge run commit(s) touch {_fmt(files)}"
                  if commits else f"no run commit modifies {_fmt(files)}")


def check_artifact(repo: str, base: str, name: str, pathspec: str) -> Result:
    out = _git(repo, "diff", "--name-only", f"{base}..HEAD", "--",
               pathspec).stdout
    paths = [ln for ln in out.splitlines() if ln.strip()]
    return Result(name, bool(paths),
                  f"committed: {_fmt(paths)}" if paths
                  else f"no run-committed file matching {pathspec}")


def journal_ids(repo: str) -> list[str]:
    out = _git(repo, "ls-tree", "--name-only", "refs/imp/journal").stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _imp(repo: str, imp: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [imp, *args], cwd=repo,
        capture_output=True, text=True, timeout=60,
    )


_MODERN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-([a-z0-9]{6})$")


def _resume_token(change_id: str) -> str:
    """6-char suffix for modern YYYY-MM-DD-xxxxxx ids; full id for legacy slugs."""
    m = _MODERN_ID.match(change_id)
    return m.group(1) if m else change_id


def check_readback(repo: str, imp: str) -> Result:
    ids = journal_ids(repo)
    if len(ids) != 1:
        return Result("readback", False,
                      f"expected exactly one journaled change, got {_fmt(ids)}")
    cid = ids[0]
    token = _resume_token(cid)
    log = _imp(repo, imp, "log", cid)
    log_full = _imp(repo, imp, "log", cid, "--full")
    status = _imp(repo, imp, "status", "--all")
    ok = (
        log.returncode == 0 and bool(log.stdout.strip())
        and log_full.returncode == 0 and bool(log_full.stdout.strip())
        and status.returncode == 0 and token in status.stdout
    )
    return Result("readback", ok,
                  f"id={cid}: log / log --full / status --all read it back"
                  if ok else
                  f"id={cid}: readback failed "
                  f"(log rc={log.returncode}, full rc={log_full.returncode}, "
                  f"status rc={status.returncode}, "
                  f"id-in-status={token in status.stdout})")


def _safe(name: str, thunk: Callable[[], Result]) -> Result:
    """Run a check, turning a stalled git/imp command into a FAIL Result."""
    try:
        return thunk()
    except subprocess.TimeoutExpired as e:
        cmd = e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd
        return Result(name, False, f"{cmd} timed out after {e.timeout}s")


def run_checks(repo: str, sentinel: str, base: str, imp: str) -> list[Result]:
    rp = _git(repo, "rev-parse", "--git-dir")
    if rp.returncode != 0:
        raise SystemExit(f"error: {repo!r} is not a git repository")
    checks: list[tuple[str, Callable[[], Result]]] = [
        ("clean_working_tree", lambda: check_clean_tree(repo)),
        ("no_imp_state_in_history", lambda: check_no_imp_in_history(repo, base)),
        ("sentinel_in_committed_src",
         lambda: check_sentinel_in_src(repo, sentinel)),
        ("atomic_coder_commit",
         lambda: check_atomic_coder_commit(repo, base, sentinel)),
        ("spec_artifact_committed",
         lambda: check_artifact(repo, base, "spec_artifact_committed",
                                "imp/specs/*.md")),
        ("plan_artifact_committed",
         lambda: check_artifact(repo, base, "plan_artifact_committed",
                                "imp/plans/*-plan-*.md")),
        ("readback", lambda: check_readback(repo, imp)),
    ]
    return [_safe(name, thunk) for name, thunk in checks]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dogfood run invariant checker.")
    ap.add_argument("repo", help="path to the completed run's repo/worktree")
    ap.add_argument("sentinel", help="the unique IMP_DOGFOOD_<hex> sentinel")
    ap.add_argument("--base", default="origin/HEAD",
                    help="pre-run base ref; run commits are base..HEAD "
                         "(default: origin/HEAD)")
    ap.add_argument("--imp", default="imp",
                    help="imp CLI to invoke for readback (default: imp)")
    ns = ap.parse_args(argv)

    results = run_checks(ns.repo, ns.sentinel, ns.base, ns.imp)
    passed = sum(r.ok for r in results)
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'} {r.name}: {r.detail}")
    all_ok = passed == len(results)
    print(f"RESULT: {'PASS' if all_ok else 'FAIL'} ({passed}/{len(results)})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
