"""Persistent repo cache — clone remote repos locally, keep them updated.

When a project repository is registered as a URL (https://github.com/...),
this module manages a local clone inside a cache directory. On each execution,
the repo is fetched to get the latest state.

Cache layout:
    {repos_dir}/{owner}/{repo}/   — bare-ish working clone

The clone is persistent across executions and container restarts
(mount the cache dir as a Docker volume).
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse


_URL_PATTERNS = (
    re.compile(r"https?://"),
    re.compile(r"git@"),
)

# Per-repo locks to prevent concurrent clone/fetch on the same cache directory
_repo_locks: dict[str, asyncio.Lock] = {}

# Pattern to strip tokens from error messages and URLs
_TOKEN_PATTERN = re.compile(r"x-access-token:[^@]+@")


def _sanitize(text: str) -> str:
    """Strip access tokens from text to prevent leaking them in logs/errors."""
    return _TOKEN_PATTERN.sub("x-access-token:***@", text)


def is_repo_url(path: str) -> bool:
    """Check if a repository path is a remote URL rather than a local path."""
    return any(p.match(path) for p in _URL_PATTERNS)


def _cache_key_from_url(url: str) -> str:
    """Extract owner/repo from a GitHub URL for use as cache directory.

    Handles:
      https://github.com/owner/repo.git → owner/repo
      https://github.com/owner/repo     → owner/repo
      git@github.com:owner/repo.git     → owner/repo
    """
    # SSH format
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)

    # HTTPS format
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


def _plain_https_url(url: str) -> str:
    """Convert any GitHub URL to plain HTTPS (no credentials).

    git@github.com:owner/repo.git → https://github.com/owner/repo.git
    https://x-access-token:xxx@github.com/... → https://github.com/...
    """
    m = re.match(r"git@github\.com:(.+)", url)
    if m:
        return f"https://github.com/{m.group(1)}"
    m = re.match(r"https://(?:[^@]+@)?github\.com/(.+)", url)
    if m:
        return f"https://github.com/{m.group(1)}"
    return url


def _build_auth_env(token: str | None) -> dict[str, str] | None:
    """Build env dict with GIT_ASKPASS for token-based auth.

    Uses GIT_ASKPASS instead of URL-embedded tokens to avoid
    leaking credentials in .git/config or error messages.
    """
    if not token:
        return None
    # GIT_ASKPASS is called by git to get the password.
    # We use a shell command that just echoes the token.
    env = {**os.environ}
    env["GIT_ASKPASS"] = "/bin/echo"
    env["GIT_USERNAME"] = "x-access-token"
    env["GIT_PASSWORD"] = token
    # For HTTPS auth, git calls GIT_ASKPASS twice: once for username, once for password.
    # Using the credential helper approach instead:
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"credential.https://github.com.helper"
    env["GIT_CONFIG_VALUE_0"] = f"!f() {{ echo username=x-access-token; echo password={token}; }}; f"
    return env


async def _run_git(
    args: list[str], cwd: str, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr).

    Stderr is sanitized to remove any accidentally embedded tokens.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        _sanitize(stderr.decode("utf-8", errors="replace").strip()),
    )


async def ensure_repo(
    repo_url: str,
    repos_dir: str,
    github_token: str | None = None,
) -> tuple[str | None, str]:
    """Ensure a local clone of a remote repo exists and is up to date.

    Uses per-repo locking to prevent concurrent clone/fetch races.
    Auth is done via env-based credential helper (not URL-embedded tokens)
    to avoid leaking tokens in .git/config or error messages.

    Args:
        repo_url: Remote repository URL (HTTPS or SSH)
        repos_dir: Base directory for cached repos (e.g. /repos or ./data/repos)
        github_token: GitHub token for authenticated clone/fetch

    Returns:
        (local_path, error_message). local_path is None on failure.
    """
    cache_key = _cache_key_from_url(repo_url)
    if not cache_key:
        return None, f"Cannot parse repo URL: {repo_url}"

    # Serialize access per repo to prevent concurrent clone/fetch races
    if cache_key not in _repo_locks:
        _repo_locks[cache_key] = asyncio.Lock()

    async with _repo_locks[cache_key]:
        return await _ensure_repo_locked(repo_url, repos_dir, cache_key, github_token)


async def _ensure_repo_locked(
    repo_url: str,
    repos_dir: str,
    cache_key: str,
    github_token: str | None,
) -> tuple[str | None, str]:
    """Inner implementation of ensure_repo, called under lock."""
    local_path = os.path.join(repos_dir, cache_key)

    # Use plain HTTPS URL for the remote (no embedded credentials)
    clean_url = _plain_https_url(repo_url)
    auth_env = _build_auth_env(github_token)

    if os.path.isdir(os.path.join(local_path, ".git")):
        # Repo exists — fetch latest
        # Ensure remote URL is clean (no embedded tokens from previous runs)
        await _run_git(["remote", "set-url", "origin", clean_url], local_path)

        code, _, err = await _run_git(["fetch", "--all", "--prune"], local_path, env=auth_env)
        if code != 0:
            return None, f"git fetch failed: {err}"

        # Reset the default branch to match remote
        code, default_branch, _ = await _run_git(
            ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"], local_path
        )
        if code == 0 and default_branch:
            branch = default_branch.replace("origin/", "")
            await _run_git(["checkout", branch], local_path)
            await _run_git(["reset", "--hard", f"origin/{branch}"], local_path)

        return local_path, ""

    # Repo doesn't exist — clone it
    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    code, _, err = await _run_git(
        ["clone", clean_url, local_path],
        cwd=os.path.dirname(local_path),
        env=auth_env,
    )
    if code != 0:
        return None, f"git clone failed: {err}"

    return local_path, ""
