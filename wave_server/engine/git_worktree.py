"""Git worktree operations — create, merge, cleanup.

Provides isolation for parallel feature execution via git worktrees.
All git operations use asyncio.create_subprocess_exec (argv-style, no shell)
to prevent command injection.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from typing import Any

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


def _branch_slug(name: str) -> str:
    """Convert a name to a safe git branch slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:50]


async def _has_uncommitted_changes(cwd: str) -> bool:
    """Check if working tree has uncommitted changes."""
    code, out, _ = await _run_git(["status", "--porcelain"], cwd)
    return code == 0 and len(out) > 0


async def _remove_worktree(repo_root: str, worktree_dir: str) -> None:
    """Remove a worktree. Best-effort, won't raise."""
    code, _, _ = await _run_git(
        ["worktree", "remove", "--force", worktree_dir], repo_root
    )
    if code != 0:
        try:
            shutil.rmtree(worktree_dir, ignore_errors=True)
        except Exception:
            pass
        await _run_git(["worktree", "prune"], repo_root)


async def _try_delete_branch(repo_root: str, branch: str) -> None:
    """Delete a branch. Best-effort, won't raise."""
    await _run_git(["branch", "-D", branch], repo_root)


async def _try_resolve_conflicts(
    cwd: str, source: str, target: str, runner: Any
) -> bool:
    """Attempt to resolve merge conflicts using an agent runner.

    Returns True if conflicts were resolved and committed.
    """
    try:
        code, conflict_output, _ = await _run_git(
            ["diff", "--name-only", "--diff-filter=U"], cwd
        )
        conflict_files = [f for f in conflict_output.split("\n") if f.strip()]
        if not conflict_files:
            return False

        from wave_server.engine.types import RunnerConfig

        prompt = (
            f"Resolve git merge conflicts in the current directory.\n"
            f'Merging branch "{source}" into "{target}".\n'
            f"Conflicted files: {', '.join(conflict_files)}\n\n"
            f"Steps:\n"
            f"1. Read each conflicted file and understand both sides\n"
            f"2. Resolve by keeping BOTH sides' changes (parallel features adding different code)\n"
            f"3. Edit each file to remove conflict markers and include all changes\n"
            f"4. Run: git add <file> for each resolved file\n"
            f"5. Run: git commit --no-edit\n\n"
            f"Do NOT delete or discard either side. Both features' code must be preserved."
        )

        config = RunnerConfig(
            task_id="merge-conflict-resolution",
            prompt=prompt,
            cwd=cwd,
            timeout_ms=120000,
        )

        result = await runner.spawn(config)

        if result.exit_code == 0:
            code, remaining, _ = await _run_git(
                ["diff", "--name-only", "--diff-filter=U"], cwd
            )
            return not remaining.strip()
    except Exception:
        pass
    return False


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
    runner: Any | None = None,
) -> list[MergeResult]:
    """Merge successful feature branches back into the current branch.

    Follows the pattern: commit → remove worktrees → merge branches.
    Attempts agent-based conflict resolution if a runner is provided.
    """
    merge_results: list[MergeResult] = []
    current_branch = await get_current_branch(repo_root) or "main"
    result_map = {r.get("name", ""): r for r in results}

    # 1. Commit changes in each successful feature worktree
    for wt in worktrees:
        r = result_map.get(wt.feature_name, {})
        if r.get("passed", False):
            if await _has_uncommitted_changes(wt.dir):
                await _run_git(["add", "-A"], wt.dir)
                await _run_git(
                    ["commit", "-m", f"pi: finalize {wt.feature_name}"],
                    wt.dir,
                )

    # 2. Remove all worktrees (frees dirs, keeps branches)
    for wt in worktrees:
        await _remove_worktree(repo_root, wt.dir)

    # 3. Merge successful feature branches into base
    for wt in worktrees:
        r = result_map.get(wt.feature_name, {})

        if not r.get("passed", False):
            # Preserve the branch — it may contain completed tasks' code
            # that a continuation/rerun can reuse.  Deleting it would
            # permanently destroy work from tasks that succeeded within
            # the failed feature.
            merge_results.append(MergeResult(
                source=wt.branch,
                target=current_branch,
                success=False,
                had_changes=False,
                error=f"Feature {wt.feature_name} failed, skipping merge (branch preserved for retry)",
            ))
            continue

        # Check if branch has changes
        code, diff_out, _ = await _run_git(
            ["log", f"{current_branch}..{wt.branch}", "--oneline"],
            repo_root,
        )
        if code == 0 and not diff_out.strip():
            await _try_delete_branch(repo_root, wt.branch)
            merge_results.append(MergeResult(
                source=wt.branch,
                target=current_branch,
                success=True,
                had_changes=False,
            ))
            continue

        code, out, err = await _run_git(
            ["merge", "--no-ff", wt.branch, "-m", f"pi: merge feature {wt.feature_name}"],
            repo_root,
        )

        if code == 0:
            await _try_delete_branch(repo_root, wt.branch)
            merge_results.append(MergeResult(
                source=wt.branch,
                target=current_branch,
                success=True,
                had_changes=True,
            ))
        else:
            # Merge conflict — try agent resolution
            resolved = False
            if runner:
                resolved = await _try_resolve_conflicts(
                    repo_root, wt.branch, current_branch, runner
                )

            if resolved:
                await _try_delete_branch(repo_root, wt.branch)
                merge_results.append(MergeResult(
                    source=wt.branch,
                    target=current_branch,
                    success=True,
                    had_changes=True,
                ))
            else:
                await _run_git(["merge", "--abort"], repo_root)
                merge_results.append(MergeResult(
                    source=wt.branch,
                    target=current_branch,
                    success=False,
                    had_changes=True,
                    error=f'Merge conflict — branch "{wt.branch}" preserved for manual resolution',
                ))

    # Clean up the .wave-worktrees directory if empty
    wt_base = os.path.join(repo_root, ".wave-worktrees")
    if os.path.isdir(wt_base):
        try:
            shutil.rmtree(wt_base, ignore_errors=True)
        except Exception:
            pass

    return merge_results


async def cleanup_worktrees(
    repo_root: str, worktrees: list[FeatureWorktree]
) -> None:
    """Remove worktrees and their branches."""
    for wt in worktrees:
        await _remove_worktree(repo_root, wt.dir)
        await _try_delete_branch(repo_root, wt.branch)


# ── Sub-Worktree Operations ───────────────────────────────────


async def create_sub_worktrees(
    feature_wt: FeatureWorktree, wave_num: int, task_ids: list[str]
) -> list[SubWorktree]:
    """Create per-task sub-worktrees branching from the feature branch.

    Each sub-worktree gets its own branch: wave-{num}/{feature}--{taskSlug}
    The double-dash separator avoids git ref hierarchy conflicts.

    Returns empty list if creation fails (caller should fall back to sequential).
    """
    sub_worktrees: list[SubWorktree] = []
    feature_slug = _branch_slug(feature_wt.feature_name)

    try:
        # Commit any uncommitted changes so sub-worktrees branch from latest state
        if await _has_uncommitted_changes(feature_wt.dir):
            await _run_git(["add", "-A"], feature_wt.dir)
            await _run_git(
                ["commit", "-m", "pi: snapshot before sub-worktree split"],
                feature_wt.dir,
            )

        for task_id in task_ids:
            task_slug = _branch_slug(task_id)
            branch = f"wave-{wave_num}/{feature_slug}--{task_slug}"
            worktree_dir = os.path.join(
                feature_wt.dir, ".wave-sub-worktrees",
                f"wave-{wave_num}", f"{feature_slug}--{task_slug}",
            )

            os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)

            code, _, err = await _run_git(
                ["worktree", "add", "-b", branch, worktree_dir, feature_wt.branch],
                feature_wt.repo_root,
            )
            if code != 0:
                raise RuntimeError(f"Failed to create sub-worktree: {err}")

            sub_worktrees.append(SubWorktree(
                task_id=task_id,
                branch=branch,
                dir=worktree_dir,
                parent_branch=feature_wt.branch,
            ))
    except Exception:
        # Partial creation — clean up and return empty (fall back to sequential)
        for sw in sub_worktrees:
            await _remove_worktree(feature_wt.repo_root, sw.dir)
            await _try_delete_branch(feature_wt.repo_root, sw.branch)
        return []

    return sub_worktrees


async def commit_task_output(
    cwd: str, task_id: str, title: str, agent: str
) -> bool:
    """Commit a task's output with a descriptive message.

    Returns True if a commit was made, False if nothing to commit or not a git repo.
    """
    try:
        if not await is_git_repo(cwd):
            return False
        if not await _has_uncommitted_changes(cwd):
            return False

        agent_tag = {"test-writer": "test", "wave-verifier": "verify"}.get(agent, "worker")
        safe_title = title.replace('"', "'")[:80]

        code, _, _ = await _run_git(["add", "-A"], cwd)
        if code != 0:
            return False

        code, _, _ = await _run_git(
            ["commit", "-m", f"pi: {task_id} [{agent_tag}] — {safe_title}"],
            cwd,
        )
        return code == 0
    except Exception:
        return False


async def merge_sub_worktrees(
    feature_wt: FeatureWorktree,
    sub_worktrees: list[SubWorktree],
    results: list[dict],
    runner: Any | None = None,
) -> list[MergeResult]:
    """Merge sub-worktree branches back into the feature branch.

    Only merges sub-worktrees whose tasks succeeded.
    Attempts agent-based conflict resolution if a runner is provided.
    """
    merge_results: list[MergeResult] = []
    result_map = {r["task_id"]: r for r in results}

    # 1. Commit changes in each successful sub-worktree
    for sw in sub_worktrees:
        r = result_map.get(sw.task_id)
        if r and r.get("exit_code", -1) == 0:
            await commit_task_output(
                sw.dir,
                sw.task_id,
                r.get("title", sw.task_id),
                r.get("agent", "worker"),
            )

    # 2. Remove all sub-worktrees (frees dirs, keeps branches)
    for sw in sub_worktrees:
        await _remove_worktree(feature_wt.repo_root, sw.dir)

    # 3. Merge successful branches into the feature branch
    for sw in sub_worktrees:
        r = result_map.get(sw.task_id)

        if not r or r.get("exit_code", -1) != 0:
            await _try_delete_branch(feature_wt.repo_root, sw.branch)
            merge_results.append(MergeResult(
                source=sw.branch,
                target=feature_wt.branch,
                success=False,
                had_changes=False,
                error="Task failed — not merged",
            ))
            continue

        # Check if branch has changes relative to parent
        code, diff_out, _ = await _run_git(
            ["log", f"{feature_wt.branch}..{sw.branch}", "--oneline"],
            feature_wt.repo_root,
        )
        if code == 0 and not diff_out.strip():
            await _try_delete_branch(feature_wt.repo_root, sw.branch)
            merge_results.append(MergeResult(
                source=sw.branch,
                target=feature_wt.branch,
                success=True,
                had_changes=False,
            ))
            continue

        # Merge into feature worktree
        code, out, err = await _run_git(
            ["merge", "--no-ff", sw.branch, "-m", f"pi: merge {sw.task_id}"],
            feature_wt.dir,
        )

        if code == 0:
            await _try_delete_branch(feature_wt.repo_root, sw.branch)
            merge_results.append(MergeResult(
                source=sw.branch,
                target=feature_wt.branch,
                success=True,
                had_changes=True,
            ))
        else:
            # Merge conflict — try agent resolution
            resolved = False
            if runner:
                resolved = await _try_resolve_conflicts(
                    feature_wt.dir, sw.branch, feature_wt.branch, runner
                )

            if resolved:
                await _try_delete_branch(feature_wt.repo_root, sw.branch)
                merge_results.append(MergeResult(
                    source=sw.branch,
                    target=feature_wt.branch,
                    success=True,
                    had_changes=True,
                ))
            else:
                await _run_git(["merge", "--abort"], feature_wt.dir)
                merge_results.append(MergeResult(
                    source=sw.branch,
                    target=feature_wt.branch,
                    success=False,
                    had_changes=True,
                    error=f'Merge conflict — branch "{sw.branch}" preserved for manual resolution',
                ))

    return merge_results


# ── Single Sub-Worktree Operations (ready-queue scheduler) ─────


async def create_single_sub_worktree(
    feature_wt: FeatureWorktree, wave_num: int, task_id: str
) -> SubWorktree | None:
    """Create one sub-worktree for a single task.

    Must be called under a lock so the feature branch state is consistent
    (i.e. all prior merges are visible to the new worktree).

    Returns None if creation fails — caller should fall back to
    running in the feature worktree directly.
    """
    feature_slug = _branch_slug(feature_wt.feature_name)
    task_slug = _branch_slug(task_id)
    branch = f"wave-{wave_num}/{feature_slug}--{task_slug}"
    worktree_dir = os.path.join(
        feature_wt.dir,
        ".wave-sub-worktrees",
        f"wave-{wave_num}",
        f"{feature_slug}--{task_slug}",
    )

    try:
        # Commit any pending changes so the sub-worktree sees latest state
        if await _has_uncommitted_changes(feature_wt.dir):
            await _run_git(["add", "-A"], feature_wt.dir)
            await _run_git(
                ["commit", "-m", "pi: snapshot before sub-worktree split"],
                feature_wt.dir,
            )

        os.makedirs(os.path.dirname(worktree_dir), exist_ok=True)

        code, _, err = await _run_git(
            ["worktree", "add", "-b", branch, worktree_dir, feature_wt.branch],
            feature_wt.repo_root,
        )
        if code != 0:
            return None

        return SubWorktree(
            task_id=task_id,
            branch=branch,
            dir=worktree_dir,
            parent_branch=feature_wt.branch,
        )
    except Exception:
        return None


async def merge_single_sub_worktree(
    feature_wt: FeatureWorktree,
    sw: SubWorktree,
    task_id: str,
    title: str,
    agent: str,
    runner: Any | None = None,
) -> MergeResult:
    """Commit, remove, and merge a single sub-worktree back into the feature branch.

    Must be called under a lock to prevent concurrent merges into the
    same feature branch.
    """
    # 1. Commit changes in the sub-worktree
    await commit_task_output(sw.dir, task_id, title, agent)

    # 2. Remove worktree (frees the directory, keeps the branch)
    await _remove_worktree(feature_wt.repo_root, sw.dir)

    # 3. Check if the branch actually has changes
    code, diff_out, _ = await _run_git(
        ["log", f"{feature_wt.branch}..{sw.branch}", "--oneline"],
        feature_wt.repo_root,
    )
    if code == 0 and not diff_out.strip():
        await _try_delete_branch(feature_wt.repo_root, sw.branch)
        return MergeResult(
            source=sw.branch,
            target=feature_wt.branch,
            success=True,
            had_changes=False,
        )

    # 4. Merge branch into the feature worktree
    code, _, err = await _run_git(
        ["merge", "--no-ff", sw.branch, "-m", f"pi: merge {sw.task_id}"],
        feature_wt.dir,
    )

    if code == 0:
        await _try_delete_branch(feature_wt.repo_root, sw.branch)
        return MergeResult(
            source=sw.branch,
            target=feature_wt.branch,
            success=True,
            had_changes=True,
        )

    # Merge conflict — try agent-based resolution
    resolved = False
    if runner:
        resolved = await _try_resolve_conflicts(
            feature_wt.dir, sw.branch, feature_wt.branch, runner
        )

    if resolved:
        await _try_delete_branch(feature_wt.repo_root, sw.branch)
        return MergeResult(
            source=sw.branch,
            target=feature_wt.branch,
            success=True,
            had_changes=True,
        )

    await _run_git(["merge", "--abort"], feature_wt.dir)
    return MergeResult(
        source=sw.branch,
        target=feature_wt.branch,
        success=False,
        had_changes=True,
        error=f'Merge conflict — branch "{sw.branch}" preserved for manual resolution',
    )


async def cleanup_single_sub_worktree(
    repo_root: str, sw: SubWorktree
) -> None:
    """Remove a single sub-worktree and its branch. Best-effort."""
    await _remove_worktree(repo_root, sw.dir)
    await _try_delete_branch(repo_root, sw.branch)


async def cleanup_sub_worktrees(
    repo_root: str, sub_worktrees: list[SubWorktree]
) -> None:
    """Remove sub-worktrees and their branches."""
    for sw in sub_worktrees:
        await _remove_worktree(repo_root, sw.dir)
        await _try_delete_branch(repo_root, sw.branch)


async def cleanup_all(
    repo_root: str,
    feature_worktrees: list[FeatureWorktree],
    sub_worktrees: list[SubWorktree] | None = None,
) -> None:
    """Emergency cleanup — remove all worktrees and branches. Best-effort."""
    if sub_worktrees:
        for sw in sub_worktrees:
            await _remove_worktree(repo_root, sw.dir)
            await _try_delete_branch(repo_root, sw.branch)

    for wt in feature_worktrees:
        await _remove_worktree(repo_root, wt.dir)
        await _try_delete_branch(repo_root, wt.branch)

    await _run_git(["worktree", "prune"], repo_root)

    # Clean up the .wave-worktrees directory if empty
    wt_base = os.path.join(repo_root, ".wave-worktrees")
    if os.path.isdir(wt_base):
        try:
            shutil.rmtree(wt_base, ignore_errors=True)
        except Exception:
            pass


# ── Execution-Level Worktree ───────────────────────────────────


async def create_execution_worktree(
    repo_root: str, branch_name: str, start_point: str
) -> tuple[str | None, str]:
    """Create a git worktree for an execution's work branch.

    Instead of checking out the work branch in the user's repo (which
    disrupts their working tree), we create a worktree under
    ``<repo>/.wave-worktrees/<branch_name>/``.

    If the branch already exists (e.g. continue/rerun), the worktree is
    attached to it.  Otherwise a new branch is created from *start_point*.

    Returns ``(worktree_dir, error_message)``.  *worktree_dir* is ``None``
    on failure.
    """
    worktree_dir = os.path.join(repo_root, ".wave-worktrees", _branch_slug(branch_name))

    # If the directory already exists from a previous run, remove it first
    if os.path.isdir(worktree_dir):
        await _remove_worktree(repo_root, worktree_dir)

    if await branch_exists(repo_root, branch_name):
        # Branch exists — attach worktree to it
        code, _, err = await _run_git(
            ["worktree", "add", worktree_dir, branch_name], repo_root
        )
    else:
        # New branch — create from start_point
        code, _, err = await _run_git(
            ["worktree", "add", "-b", branch_name, worktree_dir, start_point],
            repo_root,
        )

    if code != 0:
        return None, f"Failed to create execution worktree for {branch_name}: {err}"
    return worktree_dir, ""


async def remove_execution_worktree(repo_root: str, worktree_dir: str) -> None:
    """Remove an execution-level worktree.  Best-effort, won't raise."""
    await _remove_worktree(repo_root, worktree_dir)


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


def build_signing_env(signing_key: str) -> dict[str, str]:
    """Build env vars for git commit signing. Does NOT touch repo config.

    Auto-detects key type:
    - Paths containing / or ~ or ending in .pub → SSH signing
    - Otherwise → GPG key ID

    All config is passed via GIT_CONFIG_* env vars so it only affects
    the subprocess — your repo's .git/config is never modified.
    """
    is_ssh = "/" in signing_key or "~" in signing_key or signing_key.endswith(".pub")
    key_path = os.path.expanduser(signing_key)

    env: dict[str, str] = {}

    if is_ssh:
        env["GIT_CONFIG_COUNT"] = "3"
        env["GIT_CONFIG_KEY_0"] = "gpg.format"
        env["GIT_CONFIG_VALUE_0"] = "ssh"
        env["GIT_CONFIG_KEY_1"] = "user.signingkey"
        env["GIT_CONFIG_VALUE_1"] = key_path
        env["GIT_CONFIG_KEY_2"] = "commit.gpgsign"
        env["GIT_CONFIG_VALUE_2"] = "true"
    else:
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_0"] = "user.signingkey"
        env["GIT_CONFIG_VALUE_0"] = signing_key
        env["GIT_CONFIG_KEY_1"] = "commit.gpgsign"
        env["GIT_CONFIG_VALUE_1"] = "true"

    return env


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
