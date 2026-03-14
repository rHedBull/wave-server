---
name: write-spec
description: Write a project specification through codebase scouting and user interview. Explores the repo, asks targeted questions, and produces a structured spec document. Use when starting a new feature or project and you need a spec before planning.
---

# Write Spec

Create a specification document for a feature or project by scouting the codebase and interviewing the user.

## Inputs

The user provides:
- A **description** of what they want to build (can be brief)
- Optionally a **tier**: `hack`, `standard` (default), or `enterprise`
- Optionally an **output path** for the spec file

If no tier is specified, use `standard`.
If no output path is specified, use `docs/spec/<feature-name>.md` (derive feature-name from the description, kebab-case).

## Phase 1: Scout the Codebase

Before asking the user anything, explore the codebase to understand context. This lets you ask better questions.

### Strategy
1. **Locate relevant code** — use `find`, `rg`/`grep`, and `ls` to identify files related to the user's description
2. **Read key sections** — don't read entire files, focus on interfaces, types, exports, and patterns
3. **Follow imports** — trace 1-2 levels of dependencies to understand the architecture
4. **Check existing tests** — understand testing patterns and frameworks in use
5. **Check project config** — `package.json`, `pyproject.toml`, `Cargo.toml`, etc. for runtime, dependencies, test commands
6. **If UI exists** — read existing components, identify the design system/component library in use, note layout patterns, navigation structure, styling approach (Tailwind, CSS modules, styled-components, etc.)

Spend 1-3 minutes scouting depending on project size. Collect:
- Key files and their roles
- Existing architecture patterns
- Types/interfaces relevant to the feature
- Test framework and patterns
- Related existing features (for convention matching)
- **If UI work is involved**: existing component library, layout patterns, theme/design tokens, navigation structure

## Phase 2: Detect UI Involvement

After scouting, determine whether this feature involves **any** user interface work:
- New screens, pages, or views
- New components or modifications to existing UI
- Dashboard, form, list, detail, or visualization work
- CLI output formatting (even CLIs have UX)

If UI is involved, the interview and spec MUST include thorough UI/UX design. This is critical — underspecified UI leads to agents improvising inconsistent interfaces that fail tests and look wrong.

## Phase 3: Interview the User

Ask questions in rounds based on the tier. Incorporate what you learned from scouting — reference specific files, patterns, and types you found.

**If UI work is detected**, always include UI/UX questions regardless of tier (scaled to the tier level).

### Hack Tier
**1 round, 2-4 questions:**
- What's the main goal? (offer options based on scouting + free text)
- Any constraints or preferences?
- **If UI**: "Quick UI preference?" — offer options: minimal/plain, match existing style, or describe desired look

### Standard Tier
**1-2 rounds, 3-8 questions total:**

Round 1 (always):
- **Goal clarity**: "What's the main goal?" with options based on what you found scouting + custom option
- **Scope boundaries**: "Should this include X?" for things that could go either way
- **Key decisions**: Architectural choices if any (with pros/cons from what you found)

Round 2 (if needed, 1-3 questions):
- Clarify ambiguities from Round 1 answers
- "Any existing patterns I should follow?" (with examples you found)
- "Priority: correctness, speed, or simplicity?" if trade-offs exist

**If UI — add a UI/UX round (2-4 questions):**
- **Layout pattern**: "How should this be laid out?" — propose 2-3 concrete options based on what you scouted (e.g., "sidebar + content like the existing ProjectView, or full-width like the Dashboard?")
- **Key interactions**: "For [complex interaction], should it be: (a) drag-and-drop, (b) click-to-edit, (c) modal form?" — only ask for non-obvious interactions
- **Component library**: If the project already uses one (Cloudscape, MUI, Shadcn, etc.), confirm. If greenfield, ask: "Any preference for component library, or build custom with [detected CSS framework]?"
- **Design reference**: "Any app or UI you'd like this to feel like?" — helps anchor the visual direction

Do NOT ask about: deployment, monitoring, documentation, team conventions, CI/CD.

### Enterprise Tier
**2-3 rounds, 8-15 questions total:**

Round 1 (core):
- Problem definition and user impact
- Integration strategy (extend, replace, parallel)
- Scale and performance requirements
- Security and auth requirements

Round 2 (detail):
- Error handling approach
- API versioning needs
- Legacy support / migration
- Logging and observability

Round 3 (if needed):
- Edge cases from what you found scouting
- Cross-cutting concerns
- Rollback / feature flag strategy

**If UI — add a dedicated UI/UX round (4-8 questions):**
- **Information architecture**: What are the main screens/views? How does the user navigate between them?
- **Layout pattern**: Propose specific layouts with ASCII sketches if helpful
- **Interaction model**: For each complex interaction (forms, drag-and-drop, inline editing, filtering, real-time updates), ask how it should behave
- **Component library / design system**: Confirm or choose, plus any custom component needs
- **Visual hierarchy**: What information is most important on each screen? What's secondary?
- **States**: How should empty states, loading states, and error states look? (propose concrete options)
- **Responsive behavior**: Desktop-only, or responsive? What breakpoints matter?
- **Design reference**: "Any app or UI you'd like this to feel like?"

## Phase 4: Write the Spec

Based on scouting findings and user answers, write the spec document.

**Critical rule**: If the feature involves UI, the spec MUST contain a `## UI/UX Design` section (standard/enterprise) or at minimum a `## UI` section (hack). Agents executing the plan cannot improvise good UI — they need explicit guidance on layout, components, interactions, and states. An underspecified UI section leads to:
- Inconsistent visual patterns across components
- Missing empty/loading/error states
- Broken or untestable interactions
- Frontend test failures from mismatched expectations

### Hack Format (~30-60 lines)

```markdown
# Spec: <Title>

## What
2-3 sentences. What we're building.

## Where
- `path/to/file.ts` — what changes

## How
Brief approach. 5-10 lines max.

## UI
(Only if UI is involved. Brief but concrete.)
- Layout: [describe]
- Key components: [list with one-line descriptions]
- Main interaction: [describe]

## Done When
Bullet list of what "working" looks like.
```

### Standard Format (~100-200 lines)

```markdown
# Spec: <Title>

## Overview
3-5 sentences on what this feature does.

## Current State
Key files, how things work now. Brief.

## User Decisions
Summarize what the user chose during the interview.

## Requirements
1. FR-1: ...
2. FR-2: ...
(10-20 requirements)

## UI/UX Design
(Required if any UI work is involved. This section must be concrete enough
that an agent can build the UI without improvising visual decisions.)

### Screens / Views
For each new screen or modified view:
- **Screen name**: Purpose in one sentence
- **Layout**: Describe the layout pattern (e.g., "sidebar + scrollable content area", "full-width with sticky header and tab navigation")
- **Key components**: List each component with:
  - What it displays
  - How the user interacts with it
  - Which data it needs (reference the data model / API)

### Component Specifications
For each non-trivial component:
- **Visual structure**: What elements appear and in what arrangement (use ASCII wireframe if complex)
- **Data displayed**: Exact fields shown, formatting rules (dates, numbers, truncation)
- **Interactions**: Click targets, hover effects, drag behavior, keyboard shortcuts
- **States**: Empty state (what to show when no data), loading state, error state
- **Responsive behavior**: How it adapts at different widths (if applicable)

### Navigation & Flow
- How the user reaches each screen (sidebar link, button click, URL)
- What happens after key actions (create → redirect to detail? show toast? stay on page?)
- Back navigation behavior

### Design Patterns
- Component library / design system being used
- Consistent patterns to follow (e.g., "all forms use modal dialogs", "all lists have search + filter bar at top", "all detail pages use tabbed layout")
- Color coding conventions (e.g., status colors, priority indicators, severity badges)
- Typography hierarchy (headings, body, captions, monospace for code)

## Affected Files
- `path/to/file.ts` — what changes

## API / Interface Changes
New or changed APIs, types, signatures.

## Testing Criteria
- Test that X works when Y
- Test that error Z is handled
(5-15 test criteria, including UI component tests if applicable)

## Out of Scope
What we're explicitly NOT doing.
```

### Enterprise Format (~250-500 lines)

```markdown
# Spec: <Title>

## Overview
Detailed description of the feature, its purpose, and business impact.

## Current State
### Architecture
How the system works today in the relevant area.
### Key Files
File inventory with descriptions.
### Data Flow
How data moves through the current system.

## User Decisions
Full summary of every decision made during the interview.

## Requirements
### Functional Requirements
FR-1 through FR-N (20-40 requirements), grouped by area.
### Non-Functional Requirements
Performance, security, reliability, observability.

## UI/UX Design
(Required if any UI work is involved. Must be comprehensive — this is the
blueprint that agents follow. Every screen, every component, every interaction,
every state must be specified. Agents cannot improvise good UI.)

### Information Architecture
- Site map / screen inventory with hierarchy
- Primary navigation structure (sidebar, top nav, breadcrumbs)
- URL structure / routing

### Screen Specifications
For each screen:

#### [Screen Name]
- **Purpose**: What the user accomplishes here
- **URL**: Route path
- **Layout**: Detailed layout description
  ```
  ┌─────────────────────────────────────────────┐
  │ Header: breadcrumb + action buttons          │
  ├──────────┬──────────────────────────────────┤
  │ Sidebar  │ Main content                      │
  │ filters  │ ┌──────────────────────────────┐  │
  │          │ │ Card grid / Table / Form      │  │
  │          │ └──────────────────────────────┘  │
  └──────────┴──────────────────────────────────┘
  ```
- **Components**:
  - Component A: data, interactions, states
  - Component B: data, interactions, states
- **Empty state**: What appears when there's no data (illustration? message? CTA?)
- **Loading state**: Skeleton, spinner, or progressive loading?
- **Error state**: Inline error, error page, or toast?
- **Responsive**: Breakpoint behavior (stack sidebar below content at <768px, etc.)

### Component Library & Design System
- Which library (Cloudscape, MUI, Shadcn, Radix, Ant Design, custom, etc.)
- Theme configuration (colors, spacing, typography)
- Custom components needed beyond the library
- Icon set (Lucide, Heroicons, Material Icons, etc.)

### Interaction Patterns
- **Forms**: Inline vs modal, validation timing (on blur, on submit), error display
- **Lists/Tables**: Pagination vs infinite scroll, sort behavior, filter UX (sidebar, toolbar, dropdown)
- **Detail views**: Tab layout, collapsible sections, inline editing vs edit mode
- **Drag and drop**: What's draggable, drop targets, visual feedback during drag, reorder behavior
- **Real-time updates**: Polling, WebSocket, or manual refresh? Stale data indicators?
- **Destructive actions**: Confirmation dialog pattern (modal with typed confirmation? simple "are you sure"?)
- **Keyboard**: Tab order, keyboard shortcuts (if any), focus management in modals

### Visual Design
- Color coding: status colors (with exact values or design tokens), priority indicators, severity badges
- Typography: heading sizes, body text, captions, monospace for code/IDs
- Spacing: compact vs comfortable, card padding, list item height
- Visual hierarchy: what gets emphasis on each screen (size, color, position)

### Navigation & Flows
For each key user flow:
1. Starting point → action → result → next screen
2. Error path → what happens, where the user ends up
3. Success feedback (toast, redirect, inline confirmation)

## Integration Strategy
How this integrates with existing code. Adapter patterns, migration steps.

## Affected Files
Complete file inventory with change descriptions.

## API / Interface Changes
Full API specs with request/response shapes, error codes, versioning.

## Data Model Changes
Schema changes, migrations, backward compatibility.

## Error Handling
Error types, recovery strategies, user-facing messages.

## Testing Criteria
### Unit Tests
### Integration Tests
### E2E Tests
### UI Component Tests
- Component renders correctly with mock data
- Component renders empty state when no data
- Component renders loading state
- Interactive elements respond to clicks/input
- Form validation displays errors correctly
(20-40 test criteria)

## Security Considerations
Auth, input validation, data protection.

## Migration / Rollout
Feature flags, rollback plan, data migration.

## Out of Scope
Explicit boundaries.

## Open Questions
Anything that needs future resolution.
```

## Final Steps

1. Write the spec to the output path
2. Tell the user where the spec was saved
3. If the spec has a UI/UX Design section, offer a quick review: "Want to walk through the screen designs before we move to planning?"
4. Offer to refine any section or proceed to planning (`/skill:write-plan`)
