"""Quick-fix endpoint — single-call API for lightweight bug fixes.

POST /api/v1/projects/{project_id}/quick-fix
Runs one worker task, commits, pushes, creates PR, optionally promotes.
Returns the result synchronously.
"""
from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wave_server.config import settings
from wave_server.db import get_db
from wave_server.engine.git_worktree import (
    commit_task_output,
    create_execution_worktree,
    get_current_branch,
    get_current_sha,
    get_remote_url,
    is_git_repo,
    push_branch,
    remove_execution_worktree,
    build_signing_env,
)
from wave_server.engine.github_app import create_app_auth
from wave_server.engine.github_pr import promote_pr as github_promote_pr
from wave_server.engine.github_pr import create_pr as api_create_pr
from wave_server.engine.repo_cache import ensure_repo, is_repo_url
from wave_server.engine.runner import get_runner
from wave_server.engine.types import RunnerConfig, RunnerResult
from wave_server.models import Project, ProjectContextFile, ProjectRepository
from wave_server.schemas import QuickFixRequest, QuickFixResponse

router = APIRouter()

# Max size per context file to avoid prompt bloat (32KB)
_MAX_CONTEXT_FILE_SIZE = 32 * 1024


def _load_context_files(context_files: list, repo_cwd: str) -> str:
    """Load project context files and return combined content for prompt injection."""
    from pathlib import Path

    if not context_files:
        return ""

    sections: list[str] = []
    for cf in context_files:
        file_path = Path(cf.path)
        if not file_path.is_absolute():
            file_path = Path(repo_cwd) / file_path
        file_path = file_path.resolve()

        if not file_path.is_file():
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > _MAX_CONTEXT_FILE_SIZE:
                content = content[:_MAX_CONTEXT_FILE_SIZE] + "\n... (truncated)"
            label = cf.description or cf.path
            sections.append(f"### {label}\n```\n{content}\n```")
        except OSError:
            continue

    if not sections:
        return ""

    return "## Project Context\n\n" + "\n\n".join(sections)


def _build_quickfix_prompt(prompt: str, files: list[str], project_context: str) -> str:
    from wave_server.engine.wave_executor import _load_agent_role

    role = _load_agent_role("worker")
    files_line = f"Files: {', '.join(files)}" if files else ""
    ctx = f"\n{project_context}\n" if project_context else ""
    return f"""{role}
{ctx}
## Your Task
**Quick Fix**
{files_line}

{prompt}

- Work continuously — do NOT stop to summarize progress or wait for feedback"""


@router.post("/projects/{project_id}/quick-fix", response_model=QuickFixResponse)
async def quick_fix(
    project_id: str,
    body: QuickFixRequest,
    db: AsyncSession = Depends(get_db),
) -> QuickFixResponse:
    start_time = time.monotonic()
    repo_cwd: str | None = None
    repo_root: str | None = None
    exec_worktree_dir: str | None = None
    use_git = False

    try:
        # 1. Validate project exists
        project = await db.get(Project, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        # 1b. Has exactly one repo
        repos_result = await db.execute(
            select(ProjectRepository).where(
                ProjectRepository.project_id == project_id
            )
        )
        repos = repos_result.scalars().all()
        if not repos:
            raise HTTPException(status_code=422, detail="Project has no repository")

        repo = repos[0]

        # 2. Resolve repo path
        from pathlib import Path

        if is_repo_url(repo.path):
            # Remote repo — clone/fetch via repo cache with coding-bot token
            coding_app_auth = create_app_auth(
                app_id=settings.github_coding_app_id,
                private_key=settings.github_coding_app_key,
                installation_id=settings.github_coding_app_install_id,
            )
            clone_token = None
            if coding_app_auth:
                try:
                    clone_token = await coding_app_auth.get_token()
                except Exception:
                    pass
            repo_cwd = await ensure_repo(repo.path, token=clone_token)
            if not repo_cwd:
                raise HTTPException(status_code=422, detail="Failed to clone/fetch repository")
        else:
            if not Path(repo.path).is_dir():
                raise HTTPException(status_code=422, detail=f"Repository path not found: {repo.path}")
            repo_cwd = repo.path

        repo_root = repo_cwd

        # 3. Git setup
        use_git = await is_git_repo(repo_cwd)

        if use_git:
            current_branch = await get_current_branch(repo_cwd)
            source_branch = (
                body.source_branch
                or settings.github_pr_target
                or current_branch
                or "dev"
            )

            wt_dir, err = await create_execution_worktree(
                repo_cwd, body.branch, source_branch
            )
            if not wt_dir:
                elapsed = int((time.monotonic() - start_time) * 1000)
                return QuickFixResponse(
                    success=False,
                    branch=body.branch,
                    error=f"Cannot create work branch: {err}",
                    execution_time_ms=elapsed,
                )
            exec_worktree_dir = wt_dir
            repo_cwd = exec_worktree_dir

        # 4. Load context files
        ctx_result = await db.execute(
            select(ProjectContextFile)
            .where(ProjectContextFile.project_id == project_id)
            .order_by(ProjectContextFile.created_at)
        )
        context_files = ctx_result.scalars().all()
        project_context = _load_context_files(context_files, repo_cwd)

        # 5. Inject env vars
        project_env: dict[str, str] = {}
        if project.env_vars:
            try:
                project_env = json.loads(project.env_vars)
            except (json.JSONDecodeError, TypeError):
                pass

        if "ANTHROPIC_MODEL" not in project_env:
            project_env["ANTHROPIC_MODEL"] = settings.default_model

        github_token = project_env.get("GITHUB_TOKEN") or settings.github_token
        if github_token and "GITHUB_TOKEN" not in project_env:
            project_env["GITHUB_TOKEN"] = github_token
            project_env["GH_TOKEN"] = github_token

        coding_app_auth = create_app_auth(
            app_id=project_env.get("GITHUB_CODING_APP_ID") or settings.github_coding_app_id,
            private_key=project_env.get("GITHUB_CODING_APP_KEY") or settings.github_coding_app_key,
            installation_id=project_env.get("GITHUB_CODING_APP_INSTALL_ID") or settings.github_coding_app_install_id,
        )

        if settings.git_committer_name and "GIT_COMMITTER_NAME" not in project_env:
            project_env["GIT_COMMITTER_NAME"] = settings.git_committer_name
            project_env["GIT_AUTHOR_NAME"] = settings.git_committer_name
        if settings.git_committer_email and "GIT_COMMITTER_EMAIL" not in project_env:
            project_env["GIT_COMMITTER_EMAIL"] = settings.git_committer_email
            project_env["GIT_AUTHOR_EMAIL"] = settings.git_committer_email

        if settings.git_signing_key:
            signing_env = build_signing_env(settings.git_signing_key)
            for k, v in signing_env.items():
                if k not in project_env:
                    project_env[k] = v

        # 6. Build prompt
        prompt_text = _build_quickfix_prompt(body.prompt, body.files, project_context)

        # 7. Run worker
        runner = get_runner(settings.runtime)
        config = RunnerConfig(
            task_id="quickfix",
            prompt=prompt_text,
            cwd=repo_cwd,
            env=project_env if project_env else None,
            model=body.model,
            timeout_ms=body.timeout_ms or settings.default_timeout_ms,
        )
        result = await runner.spawn(config)

        # 8. Extract output
        output = runner.extract_final_output(result.stdout)

        elapsed = int((time.monotonic() - start_time) * 1000)

        # 9. Worker failed
        if result.exit_code != 0 or result.timed_out:
            error_msg = "Worker timed out" if result.timed_out else f"Worker failed (exit {result.exit_code})"
            if result.stderr:
                error_msg += f": {result.stderr}"
            return QuickFixResponse(
                success=False,
                branch=body.branch,
                error=error_msg,
                execution_time_ms=elapsed,
                worker_output=output,
            )

        # 10. Worker succeeded
        pr_url: str | None = None
        pr_number: int | None = None
        promoted = False
        promotion_pr_url: str | None = None

        if use_git:
            # a. Commit
            await commit_task_output(repo_cwd, "quickfix", body.pr_title, "worker")

            # b. Resolve coding-bot token
            push_token = github_token
            if coding_app_auth:
                try:
                    push_token = await coding_app_auth.get_token()
                except Exception:
                    pass

            # c. Push branch
            if push_token:
                push_ok, push_err = await push_branch(
                    repo_cwd, body.branch, github_token=push_token
                )

                # d. Create PR
                if push_ok:
                    remote_url = await get_remote_url(repo_cwd)
                    if remote_url:
                        m = re.match(
                            r"(?:https://github\.com/|git@github\.com:)([^/]+)/([^/.]+)",
                            remote_url,
                        )
                        if m:
                            owner, repo_name = m.group(1), m.group(2)
                            target_branch = (
                                body.source_branch
                                or settings.github_pr_target
                                or "dev"
                            )
                            created_url, pr_err = await api_create_pr(
                                push_token,
                                owner,
                                repo_name,
                                body.branch,
                                target_branch,
                                body.pr_title,
                                body.pr_body,
                            )
                            if created_url:
                                pr_url = created_url
                                # Extract pr_number from URL
                                pr_num_match = re.search(r"/pull/(\d+)$", pr_url)
                                if pr_num_match:
                                    pr_number = int(pr_num_match.group(1))

                                # e. Auto-promote
                                if body.auto_promote and pr_url:
                                    review_app_auth = create_app_auth(
                                        app_id=settings.github_review_app_id,
                                        private_key=settings.github_review_app_key,
                                        installation_id=settings.github_review_app_install_id,
                                    )
                                    if review_app_auth:
                                        try:
                                            review_token = await review_app_auth.get_token()
                                            promote_result = await github_promote_pr(
                                                review_token, pr_url, promotion_target="main"
                                            )
                                            if promote_result.success:
                                                promoted = True
                                                promotion_pr_url = promote_result.promotion_pr_url
                                        except Exception:
                                            pass

        return QuickFixResponse(
            success=True,
            branch=body.branch,
            pr_url=pr_url,
            pr_number=pr_number,
            promoted=promoted,
            promotion_pr_url=promotion_pr_url,
            execution_time_ms=elapsed,
            worker_output=output,
        )

    except HTTPException:
        raise
    except Exception as e:
        elapsed = int((time.monotonic() - start_time) * 1000)
        return QuickFixResponse(
            success=False,
            branch=body.branch,
            error=str(e),
            execution_time_ms=elapsed,
        )
    finally:
        # 11. Clean up worktree
        if exec_worktree_dir and repo_root:
            try:
                await remove_execution_worktree(repo_root, exec_worktree_dir)
            except Exception:
                pass
