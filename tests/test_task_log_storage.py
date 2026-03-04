"""Tests for task log CRUD operations in storage."""

import pytest

from wave_server import storage
from wave_server.config import settings


@pytest.fixture(autouse=True)
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


class TestAgentSuffix:
    def test_worker_suffix(self):
        assert storage._agent_suffix("worker") == "-impl"

    def test_test_writer_suffix(self):
        assert storage._agent_suffix("test-writer") == "-test"

    def test_verifier_suffix(self):
        assert storage._agent_suffix("wave-verifier") == "-verify"

    def test_unknown_agent_no_suffix(self):
        assert storage._agent_suffix("") == ""
        assert storage._agent_suffix("custom-agent") == ""


class TestWriteAndReadTaskLog:
    def test_write_creates_file(self):
        path = storage.write_task_log("exec-1", "t1", "# Log content", "worker")
        assert path.exists()
        assert path.name == "t1-impl.md"

    def test_read_with_agent(self):
        storage.write_task_log("exec-1", "t1", "impl log", "worker")
        assert storage.read_task_log("exec-1", "t1", "worker") == "impl log"

    def test_read_without_agent_falls_back(self):
        storage.write_task_log("exec-1", "t1", "impl log", "worker")
        assert storage.read_task_log("exec-1", "t1") == "impl log"

    def test_read_nonexistent_returns_none(self):
        assert storage.read_task_log("exec-1", "no-such-task") is None

    def test_read_nonexistent_execution_returns_none(self):
        assert storage.read_task_log("no-such-exec", "t1") is None

    def test_multiple_agents_same_task(self):
        storage.write_task_log("exec-1", "t1", "impl", "worker")
        storage.write_task_log("exec-1", "t1", "test", "test-writer")
        storage.write_task_log("exec-1", "t1", "verify", "wave-verifier")
        assert storage.read_task_log("exec-1", "t1", "worker") == "impl"
        assert storage.read_task_log("exec-1", "t1", "test-writer") == "test"
        assert storage.read_task_log("exec-1", "t1", "wave-verifier") == "verify"

    def test_overwrite_existing(self):
        storage.write_task_log("exec-1", "t1", "v1", "worker")
        storage.write_task_log("exec-1", "t1", "v2", "worker")
        assert storage.read_task_log("exec-1", "t1", "worker") == "v2"

    def test_no_agent_suffix(self):
        path = storage.write_task_log("exec-1", "t1", "bare log")
        assert path.name == "t1.md"
        assert storage.read_task_log("exec-1", "t1") == "bare log"


class TestHasTaskLog:
    def test_exists(self):
        storage.write_task_log("exec-1", "t1", "log", "worker")
        assert storage.has_task_log("exec-1", "t1") is True

    def test_not_exists(self):
        assert storage.has_task_log("exec-1", "t1") is False

    def test_nonexistent_execution(self):
        assert storage.has_task_log("no-such-exec", "t1") is False

    def test_different_task(self):
        storage.write_task_log("exec-1", "t1", "log", "worker")
        assert storage.has_task_log("exec-1", "t2") is False


class TestListTaskLogs:
    def test_empty(self):
        assert storage.list_task_logs("exec-1") == []

    def test_nonexistent_execution(self):
        assert storage.list_task_logs("no-such-exec") == []

    def test_lists_all(self):
        storage.write_task_log("exec-1", "t1", "a", "worker")
        storage.write_task_log("exec-1", "t1", "b", "test-writer")
        storage.write_task_log("exec-1", "t2", "c", "wave-verifier")
        result = storage.list_task_logs("exec-1")
        assert len(result) == 3

    def test_parses_agent_correctly(self):
        storage.write_task_log("exec-1", "t1", "a", "worker")
        storage.write_task_log("exec-1", "t2", "b", "test-writer")
        storage.write_task_log("exec-1", "t3", "c", "wave-verifier")
        storage.write_task_log("exec-1", "t4", "d")
        result = {r["task_id"]: r["agent"] for r in storage.list_task_logs("exec-1")}
        assert result["t1"] == "worker"
        assert result["t2"] == "test-writer"
        assert result["t3"] == "wave-verifier"
        assert result["t4"] == ""

    def test_sorted_by_filename(self):
        storage.write_task_log("exec-1", "t3", "c", "worker")
        storage.write_task_log("exec-1", "t1", "a", "worker")
        storage.write_task_log("exec-1", "t2", "b", "worker")
        result = storage.list_task_logs("exec-1")
        ids = [r["task_id"] for r in result]
        assert ids == ["t1", "t2", "t3"]

    def test_different_executions_isolated(self):
        storage.write_task_log("exec-1", "t1", "a", "worker")
        storage.write_task_log("exec-2", "t2", "b", "worker")
        assert len(storage.list_task_logs("exec-1")) == 1
        assert len(storage.list_task_logs("exec-2")) == 1
        assert storage.list_task_logs("exec-1")[0]["task_id"] == "t1"
