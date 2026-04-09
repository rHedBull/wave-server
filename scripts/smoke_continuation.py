#!/usr/bin/env python3
"""Manual smoke test for the continuation-recovery code path.

Exercises the edge cases of `create_execution_worktree(reset_to=…)`
and `recover_unmerged_wave_branches` against real `git` in a
throwaway directory.  Hermetic, fast (~1s), no server, no LLM cost.

Run from the wave-server repo root:

    python scripts/smoke_continuation.py

Exits 0 if every edge case passes, 1 otherwise.  Prints a per-case
report so a reviewer can eyeball what was checked.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make sure the in-tree wave_server package is importable when this
# script is run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wave_server.engine.git_worktree import (  # noqa: E402
    _has_uncommitted_changes,
    create_execution_worktree,
    recover_unmerged_wave_branches,
)


# ── helpers ────────────────────────────────────────────────────


def _git(args: str, cwd: str) -> str:
    return subprocess.run(
        ["git", *args.split()],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    _git("init -q", path)
    _git("config user.email smoke@test", path)
    _git("config user.name smoke", path)
    Path(path, "README.md").write_text("# smoke\n")
    _git("add -A", path)
    _git("commit -q -m initial", path)
    return path


def _drop_worktree(repo: str, wt: str) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", wt],
        cwd=repo,
        check=False,
        capture_output=True,
    )


# ── reporter ───────────────────────────────────────────────────


_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    _results.append((name, ok, detail))


# ── edge cases ─────────────────────────────────────────────────


async def case_e1_reset_to_pins_branch(tmp: str) -> None:
    print("\nE1: reset_to discards stale commits on existing work branch")
    repo = _init_repo(os.path.join(tmp, "e1"))
    good_sha = _git("rev-parse HEAD", repo)
    _git("branch wave/exec-aaaa", repo)

    wt, err = await create_execution_worktree(repo, "wave/exec-aaaa", good_sha)
    assert wt, err
    Path(wt, "stale.txt").write_text("stale\n")
    _git("add -A", wt)
    _git("commit -q -m stale", wt)
    stale_sha = _git("rev-parse HEAD", wt)
    _record("stale commit lands on work branch", stale_sha != good_sha, stale_sha[:8])

    _drop_worktree(repo, wt)

    wt2, err = await create_execution_worktree(
        repo, "wave/exec-aaaa", good_sha, reset_to=good_sha
    )
    assert wt2, err
    head = _git("rev-parse HEAD", wt2)
    _record("HEAD pinned back to good_sha", head == good_sha, head[:8])
    _record(
        "stale file gone from worktree",
        not Path(wt2, "stale.txt").exists(),
    )


async def case_e2_recover_orphaned_feature(tmp: str) -> None:
    print("\nE2: orphaned wave-1/<feature> branch is recovered into HEAD")
    repo = _init_repo(os.path.join(tmp, "e2"))
    base_sha = _git("rev-parse HEAD", repo)
    _git("branch wave/exec-bbbb", repo)
    _git("branch wave-1/concrete-rules", repo)

    feat = os.path.join(tmp, "e2-feat")
    subprocess.run(
        ["git", "worktree", "add", feat, "wave-1/concrete-rules"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    Path(feat, "rule_engine.py").write_text("# rule engine\n")
    _git("add -A", feat)
    _git("commit -q -m feature-work", feat)
    _drop_worktree(repo, feat)

    wt, err = await create_execution_worktree(
        repo, "wave/exec-bbbb", base_sha, reset_to=base_sha
    )
    assert wt, err
    _record(
        "before recovery: feature file absent",
        not Path(wt, "rule_engine.py").exists(),
    )

    results = await recover_unmerged_wave_branches(wt)
    _record(
        "recovery reports merged",
        results == {"wave-1/concrete-rules": "merged"},
        str(results),
    )
    _record(
        "after recovery: feature file present",
        Path(wt, "rule_engine.py").exists(),
    )


async def case_e3_already_merged_noop(tmp: str) -> None:
    print("\nE3: already-merged feature branch is a no-op")
    repo = _init_repo(os.path.join(tmp, "e3"))
    _git("branch wave/exec-cccc", repo)
    _git("branch wave-1/already", repo)

    wt, err = await create_execution_worktree(repo, "wave/exec-cccc", "HEAD")
    assert wt, err
    results = await recover_unmerged_wave_branches(wt)
    _record(
        "already-merged branch reported as 'already'",
        results == {"wave-1/already": "already"},
        str(results),
    )


async def case_e4_conflict_aborts_cleanly(tmp: str) -> None:
    print("\nE4: conflicting feature branch aborts cleanly, worktree clean")
    repo = _init_repo(os.path.join(tmp, "e4"))
    base_sha = _git("rev-parse HEAD", repo)
    _git("branch wave/exec-dddd", repo)

    wt, err = await create_execution_worktree(repo, "wave/exec-dddd", base_sha)
    assert wt, err
    Path(wt, "file.txt").write_text("work-branch version\n")
    _git("add -A", wt)
    _git("commit -q -m work", wt)

    _git(f"branch wave-1/conflicty {base_sha}", repo)
    feat = os.path.join(tmp, "e4-feat")
    subprocess.run(
        ["git", "worktree", "add", feat, "wave-1/conflicty"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    Path(feat, "file.txt").write_text("feature-branch version\n")
    _git("add -A", feat)
    _git("commit -q -m feat", feat)
    _drop_worktree(repo, feat)

    results = await recover_unmerged_wave_branches(wt)
    _record(
        "conflict reported as 'conflict'",
        results == {"wave-1/conflicty": "conflict"},
        str(results),
    )
    clean = not await _has_uncommitted_changes(wt)
    _record("worktree left clean (merge aborted)", clean)


async def case_e6_no_wave_branches_noop(tmp: str) -> None:
    print("\nE6: repo with no wave-*/* branches → recovery is a silent no-op")
    repo = _init_repo(os.path.join(tmp, "e6"))
    _git("branch wave/exec-eeee", repo)
    wt, err = await create_execution_worktree(repo, "wave/exec-eeee", "HEAD")
    assert wt, err
    results = await recover_unmerged_wave_branches(wt)
    _record("recovery returns empty dict", results == {}, str(results))


async def case_e5_glob_does_not_match_top_level(tmp: str) -> None:
    print("\nE5: top-level wave/exec-* branch is NOT picked up as a feature")
    repo = _init_repo(os.path.join(tmp, "e5"))
    # The execution's own work branch lives under refs/heads/wave/, not
    # refs/heads/wave-*/.  Make sure the for-each-ref pattern doesn't
    # accidentally try to recover-merge the work branch into itself.
    _git("branch wave/exec-ffff", repo)
    _git("branch wave-1/legit", repo)
    wt, err = await create_execution_worktree(repo, "wave/exec-ffff", "HEAD")
    assert wt, err
    results = await recover_unmerged_wave_branches(wt)
    _record(
        "only wave-1/legit considered (not wave/exec-ffff)",
        set(results.keys()) == {"wave-1/legit"},
        str(results),
    )


# ── main ───────────────────────────────────────────────────────


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave-smoke-")
    try:
        for case in (
            case_e1_reset_to_pins_branch,
            case_e2_recover_orphaned_feature,
            case_e3_already_merged_noop,
            case_e4_conflict_aborts_cleanly,
            case_e5_glob_does_not_match_top_level,
            case_e6_no_wave_branches_noop,
        ):
            try:
                await case(tmp)
            except Exception as exc:  # pragma: no cover - smoke output
                _record(f"{case.__name__} crashed", False, repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    failed = [r for r in _results if not r[1]]
    total = len(_results)
    if failed:
        print(f"❌ {len(failed)}/{total} assertions failed")
        return 1
    print(f"✅ all {total} assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
