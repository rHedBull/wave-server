"""Git worktree operations — create, merge, cleanup.

Provides isolation for parallel feature execution via git worktrees.
All git operations use asyncio.create_subprocess_exec (argv-style, no shell)
to prevent command injection.
"""

from __future__ import annotations

import asyncio
import os

from wave_server.engine.types import FeatureWorktree, MergeResult, SubWorktree


async def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a git command via create_subprocess_exec (no shell) and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def get_repo_root(cwd: str) -> str | None:
    code, out, _ = await _run_git(["rev-parse", "--show-toplevel"], cwd)
    return out if code == 0 else None


async def get_current_branch(cwd: str) -> str | None:
    code, out, _ = await _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out if code == 0 else None


async def is_git_repo(cwd: str) -> bool:
    code, _, _ = await _run_git(["rev-parse", "--git-dir"], cwd)
    return code == 0


async def create_feature_worktree(
    repo_root: str, wave_num: int, feature_name: str
) -> FeatureWorktree | None:
    """Create a git worktree for a feature branch."""
    branch = f"wave-{wave_num}/{feature_name}"
    worktree_dir = os.path.join(repo_root, ".wave-worktrees", branch)

    code, _, err = await _run_git(["branch", branch], repo_root)
    if code != 0 and "already exists" not in err:
        return None

    code, _, err = await _run_git(
        ["worktree", "add", worktree_dir, branch], repo_root
    )
    if code != 0:
        return None

    return FeatureWorktree(
        feature_name=feature_name,
        branch=branch,
        dir=worktree_dir,
        repo_root=repo_root,
    )


async def merge_feature_branches(
    repo_root: str,
    worktrees: list[FeatureWorktree],
    results: list[dict],
) -> list[MergeResult]:
    """Merge successful feature branches back into the current branch."""
    merge_results: list[MergeResult] = []
    current_branch = await get_current_branch(repo_root) or "main"

    for wt, result in zip(worktrees, results):
        if not result.get("passed", False):
            merge_results.append(
                MergeResult(
                    source=wt.branch,
                    target=current_branch,
                    success=False,
                    had_changes=False,
                    error=f"Feature {wt.feature_name} failed, skipping merge",
                )
            )
            continue

        code, out, err = await _run_git(
            ["merge", "--no-ff", wt.branch, "-m", f"Merge {wt.feature_name}"],
            repo_root,
        )

        had_changes = "Already up to date" not in out
        merge_results.append(
            MergeResult(
                source=wt.branch,
                target=current_branch,
                success=code == 0,
                had_changes=had_changes,
                error=err if code != 0 else None,
            )
        )

    return merge_results


async def cleanup_worktrees(
    repo_root: str, worktrees: list[FeatureWorktree]
) -> None:
    """Remove worktrees and their branches."""
    for wt in worktrees:
        await _run_git(["worktree", "remove", "--force", wt.dir], repo_root)
        await _run_git(["branch", "-D", wt.branch], repo_root)
