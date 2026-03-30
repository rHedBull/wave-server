"""GitHub PR operations — approve, merge, create promotion PRs.

Uses the GitHub REST API directly (httpx) rather than the gh CLI,
so operations work with GitHub App installation tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

GITHUB_API = "https://api.github.com"


@dataclass
class PRInfo:
    """Minimal PR metadata."""
    number: int
    url: str
    head_branch: str
    base_branch: str
    state: str
    merged: bool


@dataclass
class PromoteResult:
    """Result of a promote operation."""
    success: bool
    merged_pr: PRInfo | None = None
    promotion_pr_url: str | None = None
    error: str | None = None


def _parse_repo_from_url(pr_url: str) -> tuple[str, str, int] | None:
    """Extract (owner, repo, pr_number) from a GitHub PR URL.

    Handles: https://github.com/owner/repo/pull/123
    """
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_pr(token: str, owner: str, repo: str, pr_number: int) -> PRInfo | None:
    """Fetch PR info."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=_headers(token),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return PRInfo(
            number=data["number"],
            url=data["html_url"],
            head_branch=data["head"]["ref"],
            base_branch=data["base"]["ref"],
            state=data["state"],
            merged=data.get("merged", False),
        )


async def approve_pr(
    token: str, owner: str, repo: str, pr_number: int, body: str = "Approved by review-bot"
) -> tuple[bool, str]:
    """Submit an approving review on a PR. Returns (success, error)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=_headers(token),
            json={"event": "APPROVE", "body": body},
        )
        if resp.status_code in (200, 201):
            return True, ""
        return False, f"Approve failed ({resp.status_code}): {resp.text}"


async def merge_pr(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    method: str = "squash",
) -> tuple[bool, str]:
    """Merge a PR. Returns (success, error)."""
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            headers=_headers(token),
            json={"merge_method": method},
        )
        if resp.status_code == 200:
            return True, ""
        data = resp.json()
        return False, f"Merge failed ({resp.status_code}): {data.get('message', resp.text)}"


async def create_pr(
    token: str,
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> tuple[str | None, str]:
    """Create a PR. Returns (pr_url, error)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            headers=_headers(token),
            json={"head": head, "base": base, "title": title, "body": body},
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return data["html_url"], ""
        data = resp.json()
        return None, f"PR creation failed ({resp.status_code}): {data.get('message', resp.text)}"


async def promote_pr(
    review_token: str,
    pr_url: str,
    promotion_target: str,
    merge_method: str = "squash",
) -> PromoteResult:
    """Promote workflow: approve + merge a PR, then create a promotion PR to another branch.

    Args:
        review_token: GitHub token for the review-bot (must be on bypass list for PR's base branch)
        pr_url: URL of the PR to approve and merge (e.g. coding-bot's PR into dev)
        promotion_target: Branch to create the promotion PR against (e.g. "main")
        merge_method: How to merge (squash, merge, rebase)

    Returns:
        PromoteResult with merged PR info and promotion PR URL
    """
    parsed = _parse_repo_from_url(pr_url)
    if not parsed:
        return PromoteResult(success=False, error=f"Cannot parse PR URL: {pr_url}")

    owner, repo, pr_number = parsed

    # 1. Get PR info
    pr = await get_pr(review_token, owner, repo, pr_number)
    if not pr:
        return PromoteResult(success=False, error=f"PR #{pr_number} not found")

    if pr.merged:
        # Already merged — skip to promotion
        pass
    elif pr.state == "closed":
        return PromoteResult(success=False, error=f"PR #{pr_number} is closed (not merged)")
    else:
        # 2. Approve
        ok, err = await approve_pr(review_token, owner, repo, pr_number)
        if not ok:
            return PromoteResult(success=False, error=f"Approve step failed: {err}")

        # 3. Merge
        ok, err = await merge_pr(review_token, owner, repo, pr_number, merge_method)
        if not ok:
            return PromoteResult(success=False, error=f"Merge step failed: {err}")

    # 4. Create promotion PR: base_branch → promotion_target
    promo_title = f"Promote {pr.base_branch} → {promotion_target}"
    promo_body = (
        f"Automated promotion from `{pr.base_branch}` to `{promotion_target}`.\n\n"
        f"**Merged PR:** {pr.url}\n"
        f"**Source branch:** `{pr.head_branch}` → `{pr.base_branch}`\n\n"
        f"This PR requires human approval before merging."
    )

    promo_url, promo_err = await create_pr(
        review_token, owner, repo,
        head=pr.base_branch,
        base=promotion_target,
        title=promo_title,
        body=promo_body,
    )

    if not promo_url:
        # Merge succeeded but promotion PR failed — still partially successful
        return PromoteResult(
            success=True,
            merged_pr=pr,
            error=f"Merged OK but promotion PR failed: {promo_err}",
        )

    return PromoteResult(
        success=True,
        merged_pr=pr,
        promotion_pr_url=promo_url,
    )
