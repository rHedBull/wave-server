"""Tests for task log search in storage and API."""

import pytest

from wave_server import storage
from wave_server.config import settings


@pytest.fixture(autouse=True)
def tmp_storage(tmp_path, monkeypatch):
    """Redirect storage to a temp directory for all tests."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


SAMPLE_LOG_IMPL = """\
# ✅ 🔨 w1-auth-t1: Implement auth service

- **Agent**: worker
- **Phase**: feature:auth
- **Status**: passed
- **Duration**: 30.0s
- **Model**: claude-sonnet-4-20250514

---

## Execution Trace

### Turn 1

I'll implement the authentication service using JWT tokens.

**→ Read**
```
src/auth/service.py
```

### Turn 2

**→ Edit**
```
src/auth/service.py
- def login(): pass
+ def login(username, password): return create_jwt(username)
```

**→ Bash**
```
pytest tests/test_auth.py
```

## Final Output

Authentication service implemented with JWT support.
"""

SAMPLE_LOG_TEST = """\
# ❌ 🧪 w1-auth-t1: Test auth service

- **Agent**: test-writer
- **Phase**: feature:auth
- **Status**: failed (exit 1)

## Execution Trace

### Turn 1

Running the test suite for auth service.

**→ Bash**
```
pytest tests/test_auth.py -v
```

**← result (Bash) ❌ ERROR**
```
FAILED test_auth.py::test_login - AssertionError: expected 200 got 401
```

## Final Output

Tests failed: 1 failure in test_login.
"""

SAMPLE_LOG_VERIFY = """\
# ✅ 🔍 w1-db-t1: Verify database migrations

- **Agent**: wave-verifier
- **Phase**: foundation

## Execution Trace

### Turn 1

Checking database schema is correct.

**→ Bash**
```
alembic check
```

## Final Output

All migrations up to date.
"""


def _seed_logs():
    storage.write_task_log("exec-1", "w1-auth-t1", SAMPLE_LOG_IMPL, "worker")
    storage.write_task_log("exec-1", "w1-auth-t1", SAMPLE_LOG_TEST, "test-writer")
    storage.write_task_log("exec-1", "w1-db-t1", SAMPLE_LOG_VERIFY, "wave-verifier")


class TestSearchTaskLogs:
    def test_search_finds_text(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "JWT")
        assert len(results) == 1
        assert results[0]["task_id"] == "w1-auth-t1"
        assert results[0]["agent"] == "worker"
        assert results[0]["match_count"] >= 1

    def test_search_case_insensitive(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "jwt")
        assert len(results) == 1
        assert results[0]["task_id"] == "w1-auth-t1"

    def test_search_across_multiple_files(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "Bash")
        # All three logs contain "Bash"
        assert len(results) == 3

    def test_search_filter_by_agent(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "Bash", agent="worker")
        assert len(results) == 1
        assert results[0]["agent"] == "worker"

    def test_search_no_results(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "nonexistent_xyzzy")
        assert results == []

    def test_search_nonexistent_execution(self):
        results = storage.search_task_logs("no-such-exec", "test")
        assert results == []

    def test_search_error_keyword(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "ERROR")
        assert len(results) == 1
        assert results[0]["agent"] == "test-writer"

    def test_search_file_path(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "src/auth/service.py")
        assert len(results) == 1
        assert results[0]["task_id"] == "w1-auth-t1"
        assert results[0]["match_count"] >= 2  # Read + Edit both reference it

    def test_search_snippet_has_line_numbers(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "alembic")
        assert len(results) == 1
        match = results[0]["matches"][0]
        assert "line_num" in match
        assert match["line_num"] > 0
        assert "alembic" in match["snippet"]

    def test_search_long_snippet_truncated(self):
        # Write a log with a very long line
        long_line = "prefix " + "x" * 500 + " FINDME " + "y" * 500 + " suffix"
        storage.write_task_log("exec-2", "t1", f"# Log\n\n{long_line}\n", "worker")
        results = storage.search_task_logs("exec-2", "FINDME", max_context_chars=100)
        assert len(results) == 1
        snippet = results[0]["matches"][0]["snippet"]
        assert len(snippet) <= 110  # 100 + ellipses
        assert "FINDME" in snippet

    def test_search_filter_verifier(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "database", agent="wave-verifier")
        assert len(results) == 1
        assert results[0]["task_id"] == "w1-db-t1"

    def test_search_result_structure(self):
        _seed_logs()
        results = storage.search_task_logs("exec-1", "auth")
        for r in results:
            assert "task_id" in r
            assert "agent" in r
            assert "filename" in r
            assert "matches" in r
            assert "match_count" in r
            assert r["match_count"] == len(r["matches"])
            for m in r["matches"]:
                assert "line_num" in m
                assert "snippet" in m
