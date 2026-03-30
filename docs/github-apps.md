# GitHub App Integration

Wave Server uses two GitHub Apps to enforce a gated PR workflow with separated permissions. Agents can write code and create PRs, but only humans can merge into the main branch.

## Overview

```
1. Wave execution runs              ← automated (pi agents write code)
         │
         ▼
2. Coding-bot pushes branch         ← automated
   + creates PR → dev
         │
         ▼
3. POST /executions/{id}/promote    ← automated (product agent or API call)
   Review-bot approves + merges
   into dev, creates PR → main
         │
         ▼
4. Human reviews + merges → main    ← ONLY HUMAN STEP
         │
         ▼
5. GitHub Action syncs dev ← main   ← automated
```

Steps 1–3 and 5 are fully automated. The only human intervention is approving the final PR into `main`.

### Actors

| Actor | Identity | Can do | Cannot do |
|---|---|---|---|
| **Coding Bot** | `<name>-coding-bot[bot]` | Push branches, create PRs → `dev` | Merge anything |
| **Review Bot** | `<name>-review-bot[bot]` | Approve + merge PRs → `dev`, create PRs → `main` | Merge into `main` |
| **Human** | Your GitHub account | Everything | — |

### Security Model

Permissions are enforced by **GitHub repository rulesets**, not just token scopes:

- **`protect-dev` ruleset**: Requires 1 PR approval before merging. Review-bot is on the bypass list (can merge), coding-bot is not.
- **`protect-main` ruleset**: Requires 1 PR approval before merging. Only repo admins are on the bypass list.

Both apps have the same GitHub permissions (`contents: write`, `pull_requests: write`), but the rulesets control who can actually merge where.

### Token Lifecycle

GitHub Apps don't have permanent tokens. The wave server:
1. Signs a JWT with the app's private key (valid 10 minutes)
2. Exchanges it for an installation token via GitHub's API (valid 1 hour)
3. Caches the token and auto-refreshes before expiry

Handled transparently by `github_app.py` — the rest of the codebase just calls `await auth.get_token()`.

## Docker Deployment

The wave server runs inside Docker with full execution support:

| Component | How |
|---|---|
| **Pi CLI** | Installed in Docker image (Node.js 20 + npm) |
| **gh CLI** | Installed in Docker image (for PR creation) |
| **Git repos** | Cloned from GitHub into `data/repos/` volume (persistent) |
| **Pi auth** | Mounted from host: `~/.pi/agent/` → `/root/.pi/agent/` |
| **App PEM keys** | Mounted from host: `~/.config/pi-legion/` → `/secrets/pi-legion/` |
| **Git identity** | Configured in Dockerfile (`wave-bot`) |

No host filesystem access needed for repos — the wave server clones directly from GitHub using the coding-bot token.

### docker-compose.yml

```yaml
wave:
  build: ./wave-server
  ports:
    - "9718:9718"
  volumes:
    - ./data/wave:/app/data                            # DB, logs, repo cache
    - ~/.config/pi-legion:/secrets/pi-legion:ro         # PEM keys
    - ~/.pi/agent:/root/.pi/agent                       # Pi CLI auth
  environment:
    WAVE_GITHUB_CODING_APP_ID: ${WAVE_GITHUB_CODING_APP_ID}
    WAVE_GITHUB_CODING_APP_KEY: /secrets/pi-legion/coding-bot.pem
    WAVE_GITHUB_CODING_APP_INSTALL_ID: ${WAVE_GITHUB_CODING_APP_INSTALL_ID}
    WAVE_GITHUB_REVIEW_APP_ID: ${WAVE_GITHUB_REVIEW_APP_ID}
    WAVE_GITHUB_REVIEW_APP_KEY: /secrets/pi-legion/review-bot.pem
    WAVE_GITHUB_REVIEW_APP_INSTALL_ID: ${WAVE_GITHUB_REVIEW_APP_INSTALL_ID}
    WAVE_GITHUB_PR_TARGET: dev
    HOME: /root
```

## Remote Repositories

Projects register GitHub URLs instead of local paths:

```bash
curl -X POST http://localhost:9718/api/v1/projects/{id}/repositories \
  -H 'Content-Type: application/json' \
  -d '{"path": "https://github.com/owner/repo.git"}'
```

The wave server maintains a persistent local clone in `data/repos/{owner}/{repo}/`. On each execution, it fetches the latest state. Clones persist across container restarts via the Docker volume.

Supported URL formats:
- `https://github.com/owner/repo.git`
- `https://github.com/owner/repo`
- `git@github.com:owner/repo.git`

Auth uses env-based credential helpers (not URL-embedded tokens) to prevent token leaks in `.git/config` or error messages. Concurrent executions for the same repo are serialized with per-repo locking.

## API

### Promote execution

```
POST /api/v1/executions/{execution_id}/promote
Content-Type: application/json

{
  "promotion_target": "main",   // optional, default: "main"
  "merge_method": "squash"      // optional: squash | merge | rebase
}
```

Response:
```json
{
  "success": true,
  "merged_pr_url": "https://github.com/owner/repo/pull/25",
  "promotion_pr_url": "https://github.com/owner/repo/pull/26",
  "error": null
}
```

**Requirements:**
- Execution must be `completed`
- Execution must have a `pr_url` (i.e., it was pushed and a PR was created)
- Review-bot GitHub App must be configured

## Configuration

### Server-level (`.env`)

```bash
# Coding Bot — pushes branches, creates PRs into dev
WAVE_GITHUB_CODING_APP_ID=<app-id>
WAVE_GITHUB_CODING_APP_KEY=~/.config/pi-legion/coding-bot.pem  # or inline PEM
WAVE_GITHUB_CODING_APP_INSTALL_ID=<installation-id>

# Review Bot — approves/merges PRs into dev, creates PRs into main
WAVE_GITHUB_REVIEW_APP_ID=<app-id>
WAVE_GITHUB_REVIEW_APP_KEY=~/.config/pi-legion/review-bot.pem  # or inline PEM
WAVE_GITHUB_REVIEW_APP_INSTALL_ID=<installation-id>

# Default PR target branch
WAVE_GITHUB_PR_TARGET=dev
```

### Project-level overrides

Per-project settings via the project's `env_vars` override server-level config. This allows different repos to use different apps or target branches:

```json
{
  "GITHUB_CODING_APP_ID": "...",
  "GITHUB_CODING_APP_KEY": "...",
  "GITHUB_CODING_APP_INSTALL_ID": "...",
  "GITHUB_PR_TARGET": "staging"
}
```

Resolution order: **project env vars → server config → fallback** (`WAVE_GITHUB_TOKEN` PAT, source branch).

### Dev sync workflow

A GitHub Action (`.github/workflows/sync-dev.yml`) automatically merges `main` back into `dev` after each push. It uses the review-bot identity (on the `dev` bypass list) and requires:

- **Repository variable**: `REVIEW_BOT_APP_ID` = the review-bot's app ID
- **Repository secret**: `REVIEW_BOT_PRIVATE_KEY` = the review-bot's PEM key content

Set these at `Settings → Variables → Actions` and `Settings → Secrets → Actions`.

---

## Setup Guide: Adding a New Repository

Follow these steps to set up the automated PR workflow for any GitHub repository.

### Prerequisites

- A running wave server instance with GitHub Apps configured
- Admin access to the target repository

### Step 1: Install the GitHub Apps

Go to each app's settings page and install it on the target repository:

1. **https://github.com/settings/apps/\<coding-bot-name\>/installations** → Install on the repo
2. **https://github.com/settings/apps/\<review-bot-name\>/installations** → Install on the repo

Note the **installation IDs** (shown in the URL after installing, or via `gh api /app/installations`).

If the apps are already installed on your account with "All repositories" selected, skip this step.

### Step 2: Create the `dev` branch

```bash
# From the repo's default branch:
gh api /repos/OWNER/REPO/git/refs -X POST \
  -f ref="refs/heads/dev" \
  -f sha="$(gh api /repos/OWNER/REPO/git/ref/heads/main --jq '.object.sha')"
```

### Step 3: Create repository rulesets

Go to `Settings → Rules → Rulesets` in the target repo:

**Ruleset: `protect-dev`**

| Setting | Value |
|---|---|
| Target branch | `dev` |
| Require PR before merging | ✅ (1 approval) |
| Block force pushes | ✅ |
| Block deletions | ✅ |
| Bypass list | review-bot app + repo admin |

**Ruleset: `protect-main`**

| Setting | Value |
|---|---|
| Target branch | `main` (or default branch) |
| Require PR before merging | ✅ (1 approval) |
| Block force pushes | ✅ |
| Block deletions | ✅ |
| Bypass list | repo admin only |

### Step 4: Set up the dev sync workflow

Copy `.github/workflows/sync-dev.yml` into the target repo, then add:

- **Repository variable** (`Settings → Variables → Actions`): `REVIEW_BOT_APP_ID` = the review-bot's app ID
- **Repository secret** (`Settings → Secrets → Actions`): `REVIEW_BOT_PRIVATE_KEY` = the review-bot's PEM key content

### Step 5: Register the repo in wave server

```bash
# Create a project
PROJECT=$(curl -s -X POST http://localhost:9718/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-project"}' | jq -r '.id')

# Register the remote repo
curl -s -X POST "http://localhost:9718/api/v1/projects/$PROJECT/repositories" \
  -H 'Content-Type: application/json' \
  -d '{"path": "https://github.com/OWNER/REPO.git"}'
```

If the apps have different installation IDs for this repo, set project-level overrides:

```bash
curl -s -X PATCH "http://localhost:9718/api/v1/projects/$PROJECT" \
  -H 'Content-Type: application/json' \
  -d '{
    "env_vars": "{\"GITHUB_CODING_APP_INSTALL_ID\": \"NEW_ID\", \"GITHUB_REVIEW_APP_INSTALL_ID\": \"NEW_ID\"}"
  }'
```

### Step 6: Test the workflow

```bash
# Create a sequence with a simple plan
SEQ=$(curl -s -X POST "http://localhost:9718/api/v1/projects/$PROJECT/sequences" \
  -H 'Content-Type: application/json' \
  -d '{"name": "test-setup"}' | jq -r '.id')

# Upload a test plan
curl -s -X POST "http://localhost:9718/api/v1/sequences/$SEQ/plan" \
  -H 'Content-Type: text/plain' \
  -d '<!-- format: v2 -->
# Test Plan
## Goal
Test the automated PR workflow.
## Project Structure
```
repo/
├── README.md
```
## Data Schemas
N/A
## Wave 1: Test
### Foundation
#### Task 1a: Create test file
- **Agent**: worker
- **Files**: `WAVE_TEST.md`
- **Depends**: (none)
- **Description**: Create WAVE_TEST.md with "Wave server is working!"
'

# Run execution
EXEC=$(curl -s -X POST "http://localhost:9718/api/v1/sequences/$SEQ/executions" \
  -H 'Content-Type: application/json' \
  -d '{"source_branch": "dev"}' | jq -r '.id')

# Monitor until complete
watch -n 5 "curl -sf http://localhost:9718/api/v1/executions/$EXEC | jq '{status, pr_url}'"

# Once completed with a pr_url, promote:
curl -s -X POST "http://localhost:9718/api/v1/executions/$EXEC/promote" \
  -H 'Content-Type: application/json' | jq .

# → Go to GitHub and merge the promotion PR into main
```

### Checklist

After setup, verify:

- [ ] Coding-bot can push branches and create PRs → `dev`
- [ ] Coding-bot **cannot** merge PRs
- [ ] Review-bot can approve + merge PRs → `dev`
- [ ] Review-bot can create PRs → `main`
- [ ] Review-bot **cannot** merge PRs → `main`
- [ ] Only you can merge PRs → `main`
- [ ] Dev sync workflow runs after merging to main

---

## Module Reference

| Module | Purpose |
|---|---|
| `engine/github_app.py` | JWT signing, installation token generation, caching |
| `engine/github_pr.py` | GitHub REST API: get/approve/merge/create PRs, promote workflow |
| `engine/repo_cache.py` | Persistent local clones, env-based auth, per-repo locking |
| `engine/execution_manager.py` | Coding-bot token for push + PR at end of execution |
| `routes/executions.py` | `/promote` endpoint using review-bot token |
| `config.py` | All `github_*` settings with `WAVE_` env prefix |
