import pytest
from wave_server.engine.plan_parser import parse_plan, extract_data_schemas


SAMPLE_PLAN_V2 = """# Implementation Plan
<!-- format: v2 -->

## Goal

Build a user authentication system

## Data Schemas

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY,
    email TEXT UNIQUE NOT NULL
);
```

---

## Wave 1: Foundation

Set up the base infrastructure

### Foundation

#### Task w1-found-t1: Create database schema
- **Agent**: worker
- **Files**: `src/db/schema.sql`, `src/db/migrate.py`
- **Depends**: (none)
- **Description**: Create the initial database schema with users table

#### Task w1-found-t2: Write schema tests
- **Agent**: test-writer
- **Files**: `tests/test_schema.py`
- **Depends**: w1-found-t1
- **Description**: Write tests for the database schema

### Feature: Auth

Files: `src/auth/login.py`, `src/auth/register.py`

#### Task w1-auth-t1: Implement login
- **Agent**: worker
- **Files**: `src/auth/login.py`
- **Depends**: (none)
- **Description**: Implement the login endpoint

#### Task w1-auth-t2: Implement register
- **Agent**: worker
- **Files**: `src/auth/register.py`
- **Depends**: (none)
- **Description**: Implement the register endpoint

#### Task w1-auth-t3: Verify auth feature
- **Agent**: wave-verifier
- **Files**: `src/auth/login.py`, `src/auth/register.py`
- **Depends**: w1-auth-t1, w1-auth-t2
- **Description**: Verify both auth endpoints work

### Feature: Profile

#### Task w1-profile-t1: Create profile page
- **Agent**: worker
- **Files**: `src/profile/page.py`
- **Depends**: (none)
- **Description**: Create the user profile page

### Integration

#### Task w1-int-t1: Integration verification
- **Agent**: wave-verifier
- **Files**: `src/auth/login.py`, `src/profile/page.py`
- **Depends**: (none)
- **Description**: Verify all components work together
"""

SAMPLE_PLAN_LEGACY = """# Implementation Plan

## Goal

Build a simple CLI tool

## Wave 1: Core

Build the core functionality

### Task w1-t1: Create main entry point
- **Agent**: worker
- **Files**: `src/main.py`
- **Depends**: (none)
- **Description**: Create the main CLI entry point

### Task w1-t2: Add argument parsing
- **Agent**: worker
- **Files**: `src/args.py`
- **Depends**: w1-t1
- **Description**: Add argument parsing

### Task w1-t3: Verify
- **Agent**: wave-verifier
- **Depends**: w1-t1, w1-t2
- **Description**: Verify everything works
"""


def test_parse_v2_goal():
    plan = parse_plan(SAMPLE_PLAN_V2)
    assert plan.goal == "Build a user authentication system"


def test_parse_v2_data_schemas():
    plan = parse_plan(SAMPLE_PLAN_V2)
    assert "CREATE TABLE users" in plan.data_schemas


def test_parse_v2_waves():
    plan = parse_plan(SAMPLE_PLAN_V2)
    assert len(plan.waves) == 1
    wave = plan.waves[0]
    assert wave.name == "Foundation"


def test_parse_v2_foundation():
    plan = parse_plan(SAMPLE_PLAN_V2)
    wave = plan.waves[0]
    assert len(wave.foundation) == 2
    assert wave.foundation[0].id == "w1-found-t1"
    assert wave.foundation[0].agent == "worker"
    assert "src/db/schema.sql" in wave.foundation[0].files
    assert wave.foundation[1].depends == ["w1-found-t1"]


def test_parse_v2_features():
    plan = parse_plan(SAMPLE_PLAN_V2)
    wave = plan.waves[0]
    assert len(wave.features) == 2

    auth = wave.features[0]
    assert auth.name == "Auth"
    assert len(auth.files) == 2
    assert len(auth.tasks) == 3
    assert auth.tasks[2].agent == "wave-verifier"
    assert set(auth.tasks[2].depends) == {"w1-auth-t1", "w1-auth-t2"}

    profile = wave.features[1]
    assert profile.name == "Profile"
    assert len(profile.tasks) == 1


def test_parse_v2_integration():
    plan = parse_plan(SAMPLE_PLAN_V2)
    wave = plan.waves[0]
    assert len(wave.integration) == 1
    assert wave.integration[0].agent == "wave-verifier"


def test_parse_legacy_raises_error():
    """Legacy format plans (even with a version tag) should fail if no Foundation/Feature sections."""
    with pytest.raises(ValueError, match="missing a format version"):
        parse_plan(SAMPLE_PLAN_LEGACY)


def test_parse_legacy_with_version_tag():
    """Legacy format with v2 tag should parse (tasks end up in v2 parser but no sections found)."""
    tagged = "<!-- format: v2 -->\n" + SAMPLE_PLAN_LEGACY
    # v2 parser runs but finds no Foundation/Feature/Integration sections,
    # so no tasks get placed — plan parses but has empty waves
    plan = parse_plan(tagged)
    assert plan.goal == "Build a simple CLI tool"


def test_missing_version_raises_error():
    plan_no_version = """# Implementation Plan

## Goal
Do something

### Foundation

#### Task t1: Do thing
- **Agent**: worker
- **Description**: Do the thing
"""
    with pytest.raises(ValueError, match="missing a format version"):
        parse_plan(plan_no_version)


def test_unsupported_version_raises_error():
    plan_bad_version = """# Implementation Plan
<!-- format: v99 -->

## Goal
Do something
"""
    with pytest.raises(ValueError, match="Unsupported plan format 'v99'"):
        parse_plan(plan_bad_version)


def test_parse_task_description():
    plan = parse_plan(SAMPLE_PLAN_V2)
    t1 = plan.waves[0].foundation[0]
    assert "initial database schema" in t1.description


def test_extract_data_schemas():
    schemas = extract_data_schemas(SAMPLE_PLAN_V2)
    assert "CREATE TABLE users" in schemas
    assert "## Data Schemas" in schemas


def test_extract_data_schemas_empty():
    schemas = extract_data_schemas("# No schemas here\n## Wave 1: Something")
    assert schemas == ""
