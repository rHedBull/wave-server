"""Git worktree operations — create, merge, cleanup.

Provides isolation for parallel feature execution via git worktrees.
All git operations use asyncio.create_subprocess_exec (argv-style, no shell)
to prevent command injection.
"""

from __future__ import annotations

import asyncio
import os
import re

from wave_server.engine.types import FeatureWorktree, MergeResult, SubWorktree


async def _run_git(
    args: list[str], cwd: str, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a git command via create_subprocess_exec (no shell) and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
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


# ── Execution Branch Management ────────────────────────────────


async def get_current_sha(cwd: str) -> str | None:
    """Get current HEAD SHA."""
    code, out, _ = await _run_git(["rev-parse", "HEAD"], cwd)
    return out if code == 0 else None


async def branch_exists(cwd: str, branch: str) -> bool:
    """Check if a local branch exists."""
    code, _, _ = await _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd)
    return code == 0


async def sha_exists(cwd: str, sha: str) -> bool:
    """Check if a commit SHA exists in the repo."""
    code, _, _ = await _run_git(["cat-file", "-t", sha], cwd)
    return code == 0


async def create_work_branch(
    cwd: str, branch_name: str, start_point: str
) -> tuple[bool, str]:
    """Create and checkout a new work branch from a given start point.

    Returns (success, error_message).
    """
    code, _, err = await _run_git(
        ["checkout", "-b", branch_name, start_point], cwd
    )
    if code != 0:
        return False, f"Failed to create branch {branch_name}: {err}"
    return True, ""


async def checkout_branch(cwd: str, branch: str) -> tuple[bool, str]:
    """Checkout an existing branch. Returns (success, error_message)."""
    code, _, err = await _run_git(["checkout", branch], cwd)
    if code != 0:
        return False, f"Failed to checkout {branch}: {err}"
    return True, ""


async def get_remote_url(cwd: str, remote: str = "origin") -> str | None:
    """Get the URL of a remote. Returns None if remote doesn't exist."""
    code, out, _ = await _run_git(["remote", "get-url", remote], cwd)
    return out if code == 0 and out else None


def _build_git_env(github_token: str | None = None) -> dict[str, str] | None:
    """Build env dict with GitHub token for git/gh auth. Returns None if no token."""
    if not github_token:
        return None
    env = {**os.environ, "GITHUB_TOKEN": github_token, "GH_TOKEN": github_token}
    return env


def _inject_token_into_url(remote_url: str, token: str) -> str | None:
    """Convert a GitHub remote URL to HTTPS with token for authenticated push.

    Handles:
      git@github.com:owner/repo.git → https://x-access-token:{token}@github.com/owner/repo.git
      https://github.com/owner/repo.git → https://x-access-token:{token}@github.com/owner/repo.git
      https://x-access-token:old@github.com/... → replaces old token

    Returns None if the URL is not a recognized GitHub URL.
    """
    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:(.+)", remote_url)
    if m:
        return f"https://x-access-token:{token}@github.com/{m.group(1)}"

    # HTTPS with or without existing credentials
    m = re.match(r"https://(?:[^@]+@)?github\.com/(.+)", remote_url)
    if m:
        return f"https://x-access-token:{token}@github.com/{m.group(1)}"

    return None


async def push_branch(
    cwd: str,
    branch: str,
    remote: str = "origin",
    github_token: str | None = None,
) -> tuple[bool, str]:
    """Push a branch to a remote. Uses github_token for auth if provided."""
    env = _build_git_env(github_token)

    if github_token:
        # Get remote URL and inject token for authenticated HTTPS push
        remote_url = await get_remote_url(cwd, remote)
        if remote_url:
            auth_url = _inject_token_into_url(remote_url, github_token)
            if auth_url:
                # Push to the authenticated URL directly instead of the remote name
                code, out, err = await _run_git(
                    ["push", "-u", auth_url, branch], cwd, env=env
                )
                if code != 0:
                    return False, f"Failed to push {branch}: {err}"
                return True, ""

    # Fallback: push using remote name (relies on system git credentials)
    code, out, err = await _run_git(["push", "-u", remote, branch], cwd, env=env)
    if code != 0:
        return False, f"Failed to push {branch}: {err}"
    return True, ""


async def create_pr(
    cwd: str,
    work_branch: str,
    target_branch: str,
    title: str,
    body: str,
    github_token: str | None = None,
) -> tuple[str | None, str]:
    """Create a GitHub PR using the gh CLI.

    Returns (pr_url, error_message). pr_url is None on failure.
    Uses github_token for auth if provided (via GITHUB_TOKEN env var).
    """
    env = _build_git_env(github_token)

    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "create",
        "--base", target_branch,
        "--head", work_branch,
        "--title", title,
        "--body", body,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode == 0 and out:
        # gh pr create outputs the PR URL
        return out, ""
    return None, f"gh pr create failed: {err or out}"


async def has_gh_cli() -> bool:
    """Check if the gh CLI is available."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
    except FileNotFoundError:
        return False
