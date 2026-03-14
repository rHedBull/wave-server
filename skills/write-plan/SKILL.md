---
name: write-plan
description: Create a wave-based implementation plan from a spec. Reads the spec and codebase, produces a feature-parallel DAG plan ready for wave-server execution. Use after writing a spec with /skill:write-spec or when you have an existing spec file.
---

# Write Plan

Create a wave-based implementation plan from a specification document. The plan organizes work into waves with parallel features and DAG-scheduled tasks, ready for execution by the wave-server.

## Inputs

The user provides:
- A **spec file path** (e.g., `docs/spec/my-feature.md`)
- Optionally an **output path** for the plan (default: derive from spec filename)
- Optionally a **target project** on the wave-server to upload the plan to

If no output path is given, derive it from the spec filename: `spec-foo.md` → `plan-foo.md` (same directory).

## Instructions

The planning rules and output format are defined in the wave-planner agent file.

1. **Read the planner agent instructions** — the file at `../../agents/wave-planner.md` (relative to this skill) contains all the rules for plan structure, Data Schemas, API Route Tables, task description quality, FE↔BE contract rules, UI task rules, and validation steps. **Read it fully before proceeding.**

2. **Follow the planner agent's phases** exactly:
   - **Phase 1:** Read the spec and codebase (steps 1-6)
   - **Phase 2:** Write the plan (steps 7-8)
   - **Phase 3:** Validate (steps 9-15)

   But **insert an outline review step** between Phase 1 and Phase 2 (see below).

## Outline Review (between Phase 1 and Phase 2)

**Do NOT write the full plan immediately after reading the spec.** First, present an outline to the user for review.

### Create an outline

Summarize the planned structure:

1. **Wave milestones** — what each wave delivers (one line per wave)
2. **Feature parallelization** — which features run in parallel within each wave, and which files each owns
3. **API Route Table draft** — list every endpoint (method + path) grouped by feature
4. **Serialization convention** — how key format will be handled between backend and frontend
5. **Cross-wave dependencies** — what flows from wave N to wave N+1 (e.g., "Wave 1 builds backend routes → Wave 2 builds frontend API clients against those routes")
6. **Risk areas** — anything that could cause contract mismatches, temporal ordering issues, or framework gotchas

### Present and iterate

Present the outline to the user:
- "Here's the proposed plan structure. Does this look right?"
- "Would you change the milestones, move features between waves, or group things differently?"

**Wait for user feedback.** Iterate until the user approves. A plan with 50-100+ tasks is expensive to execute (~$15-30, 30-60 min). Catching structural issues in a 30-line outline is free.

When the user approves, proceed to Phase 2 (write the full plan following `../../agents/wave-planner.md`).

## Upload (Optional)

After writing and validating the plan, if the user wants to upload to the wave-server:

1. Check the wave-server is running:
   ```bash
   curl -sf http://localhost:9718/api/health | jq .
   ```

2. Find or create the project and sequence (see `/skill:wave-server` for details)

3. Upload the spec:
   ```bash
   curl -s -X POST http://localhost:9718/api/v1/sequences/<sequence_id>/spec \
     -H 'Content-Type: text/plain' \
     --data-binary @<spec-path>
   ```

4. Upload the plan:
   ```bash
   curl -s -X POST http://localhost:9718/api/v1/sequences/<sequence_id>/plan \
     -H 'Content-Type: text/plain' \
     --data-binary @<plan-path>
   ```

5. Report the sequence ID and ask if the user wants to start an execution.
