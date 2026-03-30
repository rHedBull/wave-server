# GitHub Authentication

Wave Server uses a single **bot account PAT** (Personal Access Token) for all GitHub operations: clone, push, PR creation, approve, and merge.

## Setup

1. Create a GitHub bot account (or use an existing one)
2. Generate a PAT with `repo` scope (or fine-grained with Contents + Pull Requests read/write)
3. Add the bot account as a **collaborator with write access** on any repositories you want Wave to operate on
4. Set the token in your `.env`:

```env
WAVE_GITHUB_TOKEN=ghp_xxxx
```

## How it works

The bot PAT is used in three places:

| Operation | How |
|---|---|
| **Clone/fetch repos** | Token injected into git credential for HTTPS clone |
| **Push + create PRs** | `git push` with token auth, then `gh pr create` with `GH_TOKEN` env |
| **Approve + merge** | GitHub REST API calls via httpx with token in `Authorization` header |

The same token is also injected into agent subprocess environments as `GH_TOKEN` and `GITHUB_TOKEN`, so the `gh` CLI inside agents is automatically authenticated.

## Preflight checks

Before starting an execution, the server verifies:

1. **Token exists** — returns 422 if `WAVE_GITHUB_TOKEN` is not set
2. **Repo accessible** — `GET /repos/{owner}/{repo}` succeeds
3. **Push permission** — the `permissions.push` field is `true`
4. **PR target branch exists** — if `WAVE_GITHUB_PR_TARGET` is configured

## Configuration

| Variable | Description |
|---|---|
| `WAVE_GITHUB_TOKEN` | Bot account PAT (required for remote repos) |
| `WAVE_GITHUB_PR_TARGET` | Default PR target branch (e.g. `dev`). Falls back to source branch |
| `WAVE_GIT_COMMITTER_NAME` | Git author/committer name for agent commits |
| `WAVE_GIT_COMMITTER_EMAIL` | Git author/committer email for agent commits |

## Adding a new repository

1. Add the bot account as a collaborator on the GitHub repo (Settings → Collaborators)
2. Register the repo in Wave: `POST /api/v1/projects/{id}/repositories` with `{"path": "https://github.com/owner/repo"}`
3. The preflight check will verify access before any execution starts
