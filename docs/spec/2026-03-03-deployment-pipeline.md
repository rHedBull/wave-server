# Deployment Pipeline — Spec

## Problem

The wave executor implements code and creates passing tests, but there's no defined path from "code written" to "code deployed". The handoff between agent implementation and production deployment is manual and undefined.

## Solution

An end-to-end pipeline from wave execution completion to production deployment, leveraging GitHub's native environment and protection infrastructure. Two custom review agents gate progression — everything else uses existing Git hosting capabilities.

## Flow

```
Wave execution completes (feature branch)
        |
        v
  PR: feature/* → main
        |
        v
  ┌─ Gate 1: Code Review ──────────────────────────────┐
  │  Code Review Agent (GitHub Action)                   │
  │  Focus: code quality, spec adherence, test coverage, │
  │         correctness, project conventions              │
  │  + CI checks (tests, linting, type checks, evals)    │
  └──────────────────────────────────────────────────────┘
        |
        +-- request changes → wave server notified, can re-execute
        +-- approved + CI passes → merge to main
                |
                v
          Deploy to staging (auto)
                |
                v
  ┌─ Gate 2: Deployment Review ─────────────────────────┐
  │  Deployment Review Agent (GitHub custom protection    │
  │  rule or Action, runs against live staging)           │
  │  Focus: feature completeness, availability,           │
  │         reliability, security, performance            │
  │  + Third-party gates (optional)                       │
  │    Sentry — error rates, Datadog — monitors,          │
  │    Honeycomb — latency                                │
  └──────────────────────────────────────────────────────┘
        |
        v
  ┌─ Gate 3: Human Approval ────────────────────────────┐
  │  Required reviewer on production environment          │
  └──────────────────────────────────────────────────────┘
        |
        v
  Deploy to production
```

Three stages, each with a distinct purpose:
1. **Code correctness** — is the code right? (agent + CI)
2. **Operational readiness** — does it work in a real environment? (agent + third-party tools)
3. **Human sign-off** — final go/no-go (human)

## Components

### 1. PR Creation (wave server responsibility)

When a wave execution completes successfully, the wave server:
- Creates a PR from the execution's working branch to the target branch
- PR description includes: execution ID, sequence name, spec summary, task results
- Links back to the dashboard execution view

### 2. Code Review Agent (GitHub Action)

A GitHub Action triggered on `pull_request` opened/synchronize events targeting `main`:
- Spawns a Claude Code session with a review prompt
- Reviews the full diff against the spec/plan context
- Posts a GitHub PR review via `gh pr review`:
  - **Approve** — code is correct, tests adequate, spec satisfied
  - **Request changes** — with specific comments on issues found

The review agent is a *different* agent than the one that wrote the code. It has no shared context — it reviews from scratch based on the diff, spec, and plan.

### 3. Deployment Review Agent (GitHub custom protection rule or Action)

Runs after merge to `main` triggers a staging deployment:
- Verifies the deployed staging environment is functional
- Checks feature completeness against the spec
- Evaluates availability, reliability, security posture
- Can run smoke tests, health checks, or security scans against staging

### 4. Deployment Environments (GitHub native)

Configured per-repo in GitHub:

| Environment | Trigger | Protection rules | Who approves |
|-------------|---------|-----------------|--------------|
| **staging** | Merge to `main` | Required status checks | Auto-deploy, then reviewed by deployment agent |
| **production** | Promotion from staging | Required reviewers + deployment agent approval | Human |

### 5. Third-Party Protection Rules (GitHub Apps, optional)

Existing tools gate the staging → production promotion based on operational health:
- **Sentry** — no new errors above threshold
- **Datadog** — monitors green
- **Honeycomb** — latency/error rate within bounds

These are standard GitHub App integrations — not reimplemented.

## Branch Strategy

```
feature/wave-{sequence-name}     <-- wave executor works here
        |
        v  PR (auto-created, code review agent + CI)
main                             <-- merge triggers staging deploy
        |
        v  environment: staging (deployment review agent + third-party gates)
        |
        v  environment: production (human approval)
```

Single trunk branch (`main`). No `dev` or `staging` branches — environments are deployment targets, not branches. Branch protection rules and environment associations configured in GitHub.

## Wave Server Integration Points

The wave server's role is limited:

| Responsibility | How |
|---|---|
| Create PR after execution | GitHub API (`gh pr create`) |
| Track PR status | Poll or webhook — know if PR was merged, closed, changes requested |
| Link execution to PR | Store PR URL/number on the execution record |
| React to review feedback | If changes requested, optionally trigger a new execution to address comments |
| Report deployment status | Dashboard shows PR state alongside execution state |

### New fields

```
executions table:
  + pr_url: str | None        # GitHub PR URL once created
  + pr_status: str | None     # open | merged | closed | changes_requested
```

### New API endpoints

```
POST   /api/executions/{id}/create-pr    # Create PR for completed execution
GET    /api/executions/{id}/pr-status    # Get current PR status
```

## Review Agent Details

### Code Review Agent

GitHub Action workflow (`.github/workflows/code-review.yml`):

```yaml
on:
  pull_request:
    types: [opened, synchronize]
    branches: [main]

jobs:
  code-review:
    runs-on: ubuntu-latest
    # Runs on all PRs — human and agent-generated
    steps:
      - uses: actions/checkout@v4
      - name: Run code review agent
        run: |
          claude -p "Review this PR against the spec and plan..."
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

(Simplified — actual prompt includes spec/plan context and structured output format.)

**Evaluates:**
- Does the implementation match the spec/plan?
- Are tests adequate for the changes?
- Are there obvious bugs or regressions?
- Does the code follow project conventions?

### Deployment Review Agent

GitHub Action or custom deployment protection rule, triggered after staging deployment:

**Evaluates:**
- Feature completeness — does staging behave as the spec describes?
- Availability — health checks pass, endpoints respond
- Reliability — no crash loops, error rates normal
- Security — no new vulnerabilities, auth works correctly
- Performance — response times within acceptable bounds

Can run automated checks (smoke tests, health endpoints, security scans) and/or spawn a Claude Code session to reason about the deployment.

### Labeling

PRs created by the wave server are labeled `wave-execution` for traceability (linking back to execution ID, sequence, dashboard). The code review agent runs on all PRs regardless of label — human and agent-generated alike.

## Expensive Checks (evals, load tests, AI regression)

Some checks are too slow for the PR CI gate (minutes to hours). These can run in two modes:

- **Non-blocking on PR** — runs in parallel with code review, results visible but don't block merge. Useful for early signal.
- **Blocking on staging** — runs after staging deploy, blocks production promotion. Required for high-confidence checks.

Which checks run where is configurable per-repo via GitHub Actions workflows. The deployment review agent considers all available results (both PR-level and staging-level) when making its assessment.

This is standard GitHub Actions configuration — no wave server involvement.

## What This Spec Does NOT Cover

- The review agent's detailed prompt engineering (separate concern)
- CI/CD pipeline configuration (repo-specific, standard GitHub Actions)
- Infrastructure provisioning (Vercel, AWS, etc.)
- Rollback procedures
- Monitoring/alerting setup (Sentry, Datadog config)

## Implementation Order

1. **PR creation** — wave server creates PR on execution completion (small addition to wave server)
2. **Code review agent** — GitHub Action that reviews wave-generated PRs on `main`
3. **Environment setup** — configure GitHub environments (staging + production) with protection rules
4. **Deployment review agent** — GitHub Action or custom protection rule for staging verification
5. **Dashboard integration** — show PR status and deployment status on execution detail page
6. **Feedback loop** (optional) — wave server reacts to "changes requested" by triggering re-execution
