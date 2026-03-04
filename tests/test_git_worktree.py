"""Tests for git worktree operations — create, merge, cleanup.

Uses real git repos in tmp_path for realistic testing of worktree
creation, sub-worktrees, merging, commit_task_output, and cleanup.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from wave_server.engine.git_worktree import (
    _branch_slug,
    _has_uncommitted_changes,
    cleanup_all,
    cleanup_sub_worktrees,
    cleanup_worktrees,
    commit_task_output,
    create_feature_worktree,
    create_sub_worktrees,
    get_current_branch,
    get_repo_root,
    is_git_repo,
    merge_feature_branches,
    merge_sub_worktrees,
)
from wave_server.engine.types import FeatureWorktree, SubWorktree


# ── Helpers ────────────────────────────────────────────────────


def _git(args: str, cwd: str) -> str:
    """Sync git helper for test setup."""
    return subprocess.run(
        ["git"] + args.split(),
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(path: str) -> str:
    """Create a git repo with an initial commit. Returns repo root."""
    os.makedirs(path, exist_ok=True)
    _git("init", path)
    _git("config user.email test@test.com", path)
    _git("config user.name Test", path)
    (open(os.path.join(path, "README.md"), "w")).write("# Test\n")
    _git("add -A", path)
    _git("commit -m initial", path)
    return path


# ── _branch_slug ───────────────────────────────────────────────


class TestBranchSlug:
    def test_simple_name(self):
        assert _branch_slug("auth") == "auth"

    def test_spaces_become_dashes(self):
        assert _branch_slug("my feature") == "my-feature"

    def test_special_chars_removed(self):
        assert _branch_slug("feat/auth@v2!") == "feat-auth-v2"

    def test_uppercase_lowered(self):
        assert _branch_slug("MyFeature") == "myfeature"

    def test_leading_trailing_dashes_stripped(self):
        assert _branch_slug("--auth--") == "auth"

    def test_truncated_to_50_chars(self):
        long_name = "a" * 100
        assert len(_branch_slug(long_name)) == 50

    def test_empty_string(self):
        assert _branch_slug("") == ""

    def test_task_id_format(self):
        assert _branch_slug("w1-auth-t3") == "w1-auth-t3"


# ── is_git_repo / get_repo_root / get_current_branch ──────────


class TestGitBasics:
    @pytest.mark.asyncio
    async def test_is_git_repo_true(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        assert await is_git_repo(repo) is True

    @pytest.mark.asyncio
    async def test_is_git_repo_false(self, tmp_path):
        non_repo = str(tmp_path / "not-a-repo")
        os.makedirs(non_repo)
        assert await is_git_repo(non_repo) is False

    @pytest.mark.asyncio
    async def test_get_repo_root(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        root = await get_repo_root(repo)
        assert root == repo

    @pytest.mark.asyncio
    async def test_get_current_branch(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        branch = await get_current_branch(repo)
        assert branch in ("main", "master")


# ── _has_uncommitted_changes ───────────────────────────────────


class TestUncommittedChanges:
    @pytest.mark.asyncio
    async def test_clean_repo(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        assert await _has_uncommitted_changes(repo) is False

    @pytest.mark.asyncio
    async def test_dirty_repo(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        (open(os.path.join(repo, "new.txt"), "w")).write("new file")
        assert await _has_uncommitted_changes(repo) is True


# ── commit_task_output ─────────────────────────────────────────


class TestCommitTaskOutput:
    @pytest.mark.asyncio
    async def test_commits_changes(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        (open(os.path.join(repo, "app.py"), "w")).write("print('hello')")
        result = await commit_task_output(repo, "t1", "Setup app", "worker")
        assert result is True
        # Verify commit message
        log = _git("log --oneline -1", repo)
        assert "pi: t1 [worker]" in log
        assert "Setup app" in log

    @pytest.mark.asyncio
    async def test_no_changes_returns_false(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        result = await commit_task_output(repo, "t1", "No-op", "worker")
        assert result is False

    @pytest.mark.asyncio
    async def test_non_git_dir_returns_false(self, tmp_path):
        non_repo = str(tmp_path / "not-a-repo")
        os.makedirs(non_repo)
        result = await commit_task_output(non_repo, "t1", "Test", "worker")
        assert result is False

    @pytest.mark.asyncio
    async def test_agent_tag_test_writer(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        (open(os.path.join(repo, "test.py"), "w")).write("assert True")
        await commit_task_output(repo, "t2", "Write tests", "test-writer")
        log = _git("log --oneline -1", repo)
        assert "[test]" in log

    @pytest.mark.asyncio
    async def test_agent_tag_verifier(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        (open(os.path.join(repo, "verify.txt"), "w")).write("ok")
        await commit_task_output(repo, "t3", "Verify", "wave-verifier")
        log = _git("log --oneline -1", repo)
        assert "[verify]" in log


# ── create_feature_worktree ────────────────────────────────────


class TestCreateFeatureWorktree:
    @pytest.mark.asyncio
    async def test_creates_worktree_and_branch(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt is not None
        assert wt.feature_name == "auth"
        assert wt.branch == "wave-1/auth"
        assert os.path.isdir(wt.dir)
        assert wt.repo_root == repo
        # Branch exists
        branches = _git("branch", repo)
        assert "wave-1/auth" in branches
        # Cleanup
        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_worktree_has_repo_content(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "billing")
        assert wt is not None
        assert os.path.isfile(os.path.join(wt.dir, "README.md"))
        await cleanup_worktrees(repo, [wt])

    @pytest.mark.asyncio
    async def test_multiple_features(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        wt1 = await create_feature_worktree(repo, 1, "auth")
        wt2 = await create_feature_worktree(repo, 1, "billing")
        assert wt1 is not None
        assert wt2 is not None
        assert wt1.dir != wt2.dir
        assert wt1.branch != wt2.branch
        await cleanup_worktrees(repo, [wt1, wt2])

    @pytest.mark.asyncio
    async def test_non_git_returns_none(self, tmp_path):
        non_repo = str(tmp_path / "not-a-repo")
        os.makedirs(non_repo)
        wt = await create_feature_worktree(non_repo, 1, "auth")
        assert wt is None


# ── create_sub_worktrees ──────────────────────────────────────


class TestCreateSubWorktrees:
    @pytest.mark.asyncio
    async def test_creates_sub_worktrees(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt is not None

        subs = await create_sub_worktrees(feature_wt, 1, ["t1", "t2"])
        assert len(subs) == 2
        assert subs[0].task_id == "t1"
        assert subs[1].task_id == "t2"
        assert os.path.isdir(subs[0].dir)
        assert os.path.isdir(subs[1].dir)
        assert subs[0].parent_branch == feature_wt.branch
        assert subs[1].parent_branch == feature_wt.branch
        # Each has the README from repo
        assert os.path.isfile(os.path.join(subs[0].dir, "README.md"))
        # Branch names use double-dash separator
        assert "--" in subs[0].branch
        # Cleanup
        await cleanup_sub_worktrees(repo, subs)
        await cleanup_worktrees(repo, [feature_wt])

    @pytest.mark.asyncio
    async def test_sub_worktrees_have_feature_content(self, tmp_path):
        """Sub-worktrees should include any uncommitted changes from the feature."""
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt is not None

        # Write a file in the feature worktree
        with open(os.path.join(feature_wt.dir, "auth.py"), "w") as f:
            f.write("# auth code")

        subs = await create_sub_worktrees(feature_wt, 1, ["t1", "t2"])
        assert len(subs) == 2
        # Sub-worktrees should have the committed auth.py
        assert os.path.isfile(os.path.join(subs[0].dir, "auth.py"))
        assert os.path.isfile(os.path.join(subs[1].dir, "auth.py"))
        # Cleanup
        await cleanup_sub_worktrees(repo, subs)
        await cleanup_worktrees(repo, [feature_wt])

    @pytest.mark.asyncio
    async def test_single_task_still_works(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt is not None

        subs = await create_sub_worktrees(feature_wt, 1, ["t1"])
        assert len(subs) == 1
        await cleanup_sub_worktrees(repo, subs)
        await cleanup_worktrees(repo, [feature_wt])


# ── merge_sub_worktrees ───────────────────────────────────────


class TestMergeSubWorktrees:
    @pytest.mark.asyncio
    async def test_merges_successful_sub_worktrees(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt is not None

        subs = await create_sub_worktrees(feature_wt, 1, ["t1", "t2"])
        assert len(subs) == 2

        # Write different files in each sub-worktree
        with open(os.path.join(subs[0].dir, "login.py"), "w") as f:
            f.write("# login")
        with open(os.path.join(subs[1].dir, "register.py"), "w") as f:
            f.write("# register")

        results = [
            {"task_id": "t1", "exit_code": 0, "title": "Login", "agent": "worker"},
            {"task_id": "t2", "exit_code": 0, "title": "Register", "agent": "worker"},
        ]
        merge_results = await merge_sub_worktrees(feature_wt, subs, results)

        assert len(merge_results) == 2
        assert all(mr.success for mr in merge_results)

        # Feature worktree should now have both files
        assert os.path.isfile(os.path.join(feature_wt.dir, "login.py"))
        assert os.path.isfile(os.path.join(feature_wt.dir, "register.py"))

        await cleanup_worktrees(repo, [feature_wt])

    @pytest.mark.asyncio
    async def test_failed_task_not_merged(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt is not None

        subs = await create_sub_worktrees(feature_wt, 1, ["t1", "t2"])
        assert len(subs) == 2

        with open(os.path.join(subs[0].dir, "good.py"), "w") as f:
            f.write("# good")
        with open(os.path.join(subs[1].dir, "bad.py"), "w") as f:
            f.write("# bad")

        results = [
            {"task_id": "t1", "exit_code": 0, "title": "Good", "agent": "worker"},
            {"task_id": "t2", "exit_code": 1, "title": "Bad", "agent": "worker"},
        ]
        merge_results = await merge_sub_worktrees(feature_wt, subs, results)

        assert merge_results[0].success is True
        assert merge_results[1].success is False
        assert "failed" in merge_results[1].error.lower()

        # Only good.py should be in feature
        assert os.path.isfile(os.path.join(feature_wt.dir, "good.py"))
        assert not os.path.isfile(os.path.join(feature_wt.dir, "bad.py"))

        await cleanup_worktrees(repo, [feature_wt])

    @pytest.mark.asyncio
    async def test_no_changes_sub_worktree(self, tmp_path):
        """Sub-worktree with no changes should merge as success with had_changes=False."""
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt is not None

        subs = await create_sub_worktrees(feature_wt, 1, ["t1"])
        assert len(subs) == 1

        # Don't write any files in the sub-worktree
        results = [{"task_id": "t1", "exit_code": 0, "title": "Noop", "agent": "worker"}]
        merge_results = await merge_sub_worktrees(feature_wt, subs, results)

        assert len(merge_results) == 1
        assert merge_results[0].success is True
        assert merge_results[0].had_changes is False

        await cleanup_worktrees(repo, [feature_wt])


# ── merge_feature_branches ────────────────────────────────────


class TestMergeFeatureBranches:
    @pytest.mark.asyncio
    async def test_merges_passing_features(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))

        wt1 = await create_feature_worktree(repo, 1, "auth")
        wt2 = await create_feature_worktree(repo, 1, "billing")
        assert wt1 and wt2

        # Write files in each worktree
        with open(os.path.join(wt1.dir, "auth.py"), "w") as f:
            f.write("# auth")
        _git("add -A", wt1.dir)
        _git("commit -m auth", wt1.dir)

        with open(os.path.join(wt2.dir, "billing.py"), "w") as f:
            f.write("# billing")
        _git("add -A", wt2.dir)
        _git("commit -m billing", wt2.dir)

        results = [
            {"name": "auth", "passed": True},
            {"name": "billing", "passed": True},
        ]
        merge_results = await merge_feature_branches(repo, [wt1, wt2], results)

        assert len(merge_results) == 2
        assert all(mr.success for mr in merge_results)

        # Main repo should have both files
        assert os.path.isfile(os.path.join(repo, "auth.py"))
        assert os.path.isfile(os.path.join(repo, "billing.py"))

    @pytest.mark.asyncio
    async def test_failed_feature_not_merged(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))

        wt1 = await create_feature_worktree(repo, 1, "auth")
        wt2 = await create_feature_worktree(repo, 1, "billing")
        assert wt1 and wt2

        with open(os.path.join(wt1.dir, "auth.py"), "w") as f:
            f.write("# auth")
        _git("add -A", wt1.dir)
        _git("commit -m auth", wt1.dir)

        with open(os.path.join(wt2.dir, "billing.py"), "w") as f:
            f.write("# billing")
        _git("add -A", wt2.dir)
        _git("commit -m billing", wt2.dir)

        results = [
            {"name": "auth", "passed": True},
            {"name": "billing", "passed": False},
        ]
        merge_results = await merge_feature_branches(repo, [wt1, wt2], results)

        assert merge_results[0].success is True
        assert merge_results[1].success is False

        assert os.path.isfile(os.path.join(repo, "auth.py"))
        assert not os.path.isfile(os.path.join(repo, "billing.py"))

    @pytest.mark.asyncio
    async def test_no_changes_feature(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))

        wt = await create_feature_worktree(repo, 1, "noop")
        assert wt

        results = [{"name": "noop", "passed": True}]
        merge_results = await merge_feature_branches(repo, [wt], results)

        assert len(merge_results) == 1
        assert merge_results[0].success is True
        assert merge_results[0].had_changes is False


# ── cleanup_all ────────────────────────────────────────────────


class TestCleanupAll:
    @pytest.mark.asyncio
    async def test_cleans_feature_worktrees(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        wt1 = await create_feature_worktree(repo, 1, "auth")
        wt2 = await create_feature_worktree(repo, 1, "billing")
        assert wt1 and wt2

        await cleanup_all(repo, [wt1, wt2])

        # Worktree dirs should be gone
        assert not os.path.isdir(wt1.dir)
        assert not os.path.isdir(wt2.dir)
        # Branches should be gone
        branches = _git("branch", repo)
        assert "wave-1/auth" not in branches
        assert "wave-1/billing" not in branches

    @pytest.mark.asyncio
    async def test_cleans_sub_worktrees_too(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        feature_wt = await create_feature_worktree(repo, 1, "auth")
        assert feature_wt
        subs = await create_sub_worktrees(feature_wt, 1, ["t1", "t2"])
        assert len(subs) == 2

        await cleanup_all(repo, [feature_wt], subs)

        assert not os.path.isdir(feature_wt.dir)
        for sw in subs:
            assert not os.path.isdir(sw.dir)

    @pytest.mark.asyncio
    async def test_idempotent(self, tmp_path):
        """Calling cleanup_all twice should not raise."""
        repo = _init_repo(str(tmp_path / "repo"))
        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        await cleanup_all(repo, [wt])
        await cleanup_all(repo, [wt])  # Second call should not raise


# ── Full lifecycle: create → work → merge → cleanup ───────────


class TestFullWorktreeLifecycle:
    @pytest.mark.asyncio
    async def test_two_features_with_sub_worktrees(self, tmp_path):
        """End-to-end: 2 features, each with 2 parallel tasks via sub-worktrees."""
        repo = _init_repo(str(tmp_path / "repo"))

        # Create feature worktrees
        auth_wt = await create_feature_worktree(repo, 1, "auth")
        billing_wt = await create_feature_worktree(repo, 1, "billing")
        assert auth_wt and billing_wt

        # Create sub-worktrees for auth
        auth_subs = await create_sub_worktrees(auth_wt, 1, ["a1", "a2"])
        assert len(auth_subs) == 2

        # Write code in each sub-worktree
        with open(os.path.join(auth_subs[0].dir, "login.py"), "w") as f:
            f.write("def login(): pass")
        with open(os.path.join(auth_subs[1].dir, "register.py"), "w") as f:
            f.write("def register(): pass")

        # Merge sub-worktrees → auth feature branch
        sub_results = [
            {"task_id": "a1", "exit_code": 0, "title": "Login", "agent": "worker"},
            {"task_id": "a2", "exit_code": 0, "title": "Register", "agent": "worker"},
        ]
        sub_merge = await merge_sub_worktrees(auth_wt, auth_subs, sub_results)
        assert all(mr.success for mr in sub_merge)

        # Write code in billing (no sub-worktrees)
        with open(os.path.join(billing_wt.dir, "invoice.py"), "w") as f:
            f.write("def create_invoice(): pass")
        _git("add -A", billing_wt.dir)
        _git("commit -m invoice", billing_wt.dir)

        # Merge feature branches → main
        feature_results = [
            {"name": "auth", "passed": True},
            {"name": "billing", "passed": True},
        ]
        feature_merge = await merge_feature_branches(
            repo, [auth_wt, billing_wt], feature_results
        )
        assert all(mr.success for mr in feature_merge)

        # Main repo has everything
        assert os.path.isfile(os.path.join(repo, "login.py"))
        assert os.path.isfile(os.path.join(repo, "register.py"))
        assert os.path.isfile(os.path.join(repo, "invoice.py"))

    @pytest.mark.asyncio
    async def test_original_branch_preserved(self, tmp_path):
        """The original branch should still be checked out after feature work."""
        repo = _init_repo(str(tmp_path / "repo"))
        original = await get_current_branch(repo)

        wt = await create_feature_worktree(repo, 1, "auth")
        assert wt

        # Main repo should still be on original branch
        current = await get_current_branch(repo)
        assert current == original

        await cleanup_worktrees(repo, [wt])
        current = await get_current_branch(repo)
        assert current == original
