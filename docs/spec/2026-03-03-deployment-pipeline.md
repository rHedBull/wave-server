# Deployment Pipeline — Spec

## Problem

The wave executor implements code and creates passing tests, but there's no defined path from "code written" to "code deployed". The handoff between agent implementation and production deployment is manual and undefined.

## Solution

An end-to-end pipeline from wave execution completion to production deployment, leveraging GitHub's native environment and protection infrastructure. The only custom piece is an automated review agent — everything else uses existing Git hosting capabilities.

## Flow

```
Wave execution completes
        |
        v
  PR created (by wave server)
        |
        v
  Review agent auto-reviews PR
  (GitHub Action, spawns Claude Code)
        |
        +-- request changes --> wave server notified, can re-execute
        +-- approved -->
                |
                v
          CI checks run
          (tests, linting, type checks, evals)
                |
                v
          GitHub Environment gates
          |-- dev:      auto-merge if checks pass
          |-- staging:  agent-approved (custom protection rule)
          |-- prod:     human-approved (required reviewers)
                |
                v
          Third-party gates (optional)
          (Sentry, Datadog, Honeycomb — error rates, monitors)
                |
                v
          Deployed
```

## Components

### 1. PR Creation (wave server responsibility)

When a wave execution completes successfully, the wave server:
- Creates a PR from the execution's working branch to the target branch
- PR description includes: execution ID, sequence name, spec summary, task results
- Links back to the dashboard execution view

### 2. Review Agent (GitHub Action)

A GitHub Action triggered on `pull_request` opened/synchronize events:
- Spawns a Claude Code session with a review prompt
- Reviews the full diff against the spec/plan context
- Posts a GitHub PR review via `gh pr review`:
  - **Approve** — code is correct, tests adequate, spec satisfied
  - **Request changes** — with specific comments on issues found

The review agent is a *different* agent than the one that wrote the code. It has no shared context — it reviews from scratch based on the diff, spec, and plan.

### 3. Deployment Environments (GitHub native)

Configured per-repo in GitHub:

| Environment | Protection rules | Who approves |
|-------------|-----------------|--------------|
| **dev** | Required status checks (CI passes) | Auto-merge |
| **staging** | Required status checks + wait timer (optional) | Agent or auto |
| **production** | Required status checks + required reviewers | Human |

### 4. Third-Party Protection Rules (GitHub Apps, optional)

Existing tools gate deployments based on operational health:
- **Sentry** — no new errors above threshold
- **Datadog** — monitors green
- **Honeycomb** — latency/error rate within bounds

These are standard GitHub App integrations — not reimplemented.

## Branch Strategy

```
feature/wave-{sequence-name}     <-- wave executor works here
        |
        v  PR (auto-created)
develop / main                   <-- review agent reviews, CI runs
        |
        v  environment: staging
staging branch (if used)
        |
        v  environment: production (human approval)
production
```

Branch protection rules, merge requirements, and environment associations are configured in GitHub — not in the wave server.

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

### Implementation

GitHub Action workflow (`.github/workflows/agent-review.yml`):

```yaml
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  agent-review:
    runs-on: ubuntu-latest
    if: contains(github.event.pull_request.labels.*.name, 'wave-execution')
    steps:
      - uses: actions/checkout@v4
      - name: Run review agent
        run: |
          claude --output-format stream-json -p "Review this PR: $(gh pr diff ${{ github.event.pull_request.number }})"
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

(Simplified — actual prompt would include spec/plan context, review criteria, and structured output format for the review.)

### Review Criteria

The review agent evaluates:
- Does the implementation match the spec/plan?
- Are tests adequate for the changes?
- Are there obvious bugs, security issues, or regressions?
- Does the code follow project conventions?

### Labeling

PRs created by the wave server are labeled `wave-execution` so the review action only triggers on agent-generated PRs (not human PRs, unless desired).

## What This Spec Does NOT Cover

- The review agent's detailed prompt engineering (separate concern)
- CI/CD pipeline configuration (repo-specific, standard GitHub Actions)
- Infrastructure provisioning (Vercel, AWS, etc.)
- Rollback procedures
- Monitoring/alerting setup (Sentry, Datadog config)

## Implementation Order

1. **PR creation** — wave server creates PR on execution completion (small addition to wave server)
2. **Review agent** — GitHub Action that reviews wave-generated PRs
3. **Environment setup** — configure GitHub environments with appropriate protection rules
4. **Dashboard integration** — show PR status on execution detail page
5. **Feedback loop** (optional) — wave server reacts to "changes requested" by triggering re-execution
