"""Tests for storage module — file I/O for specs, plans, output, transcripts, logs."""

from pathlib import Path
from unittest.mock import patch

import pytest

from wave_server.config import Settings
from wave_server import storage


@pytest.fixture(autouse=True)
def mock_storage_dir(tmp_path):
    """Redirect all storage operations to a temp directory."""
    mock_settings = Settings(data_dir=tmp_path)
    with patch("wave_server.storage.settings", mock_settings):
        yield tmp_path


# ── Specs ──────────────────────────────────────────────────────


class TestSpecs:
    def test_write_and_read(self):
        storage.write_spec("seq-1", "# My Spec\nDetails here.")
        content = storage.read_spec("seq-1")
        assert content == "# My Spec\nDetails here."

    def test_read_missing_returns_none(self):
        assert storage.read_spec("nonexistent") is None

    def test_write_creates_directories(self, mock_storage_dir):
        storage.write_spec("seq-2", "content")
        path = mock_storage_dir / "storage" / "specs" / "seq-2" / "spec.md"
        assert path.exists()

    def test_path_structure(self, mock_storage_dir):
        path = storage.spec_path("my-seq")
        expected = mock_storage_dir / "storage" / "specs" / "my-seq" / "spec.md"
        assert path == expected

    def test_overwrite_existing(self):
        storage.write_spec("seq-1", "version 1")
        storage.write_spec("seq-1", "version 2")
        assert storage.read_spec("seq-1") == "version 2"

    def test_unicode_content(self):
        content = "# Spécification\n\n日本語テスト\n🚀 emoji"
        storage.write_spec("seq-unicode", content)
        assert storage.read_spec("seq-unicode") == content


# ── Plans ──────────────────────────────────────────────────────


class TestPlans:
    def test_write_and_read(self):
        storage.write_plan("seq-1", "## Wave 1: Setup")
        assert storage.read_plan("seq-1") == "## Wave 1: Setup"

    def test_read_missing_returns_none(self):
        assert storage.read_plan("nonexistent") is None

    def test_path_structure(self, mock_storage_dir):
        path = storage.plan_path("my-seq")
        expected = mock_storage_dir / "storage" / "plans" / "my-seq" / "plan.md"
        assert path == expected

    def test_write_creates_directories(self, mock_storage_dir):
        storage.write_plan("seq-3", "content")
        path = mock_storage_dir / "storage" / "plans" / "seq-3" / "plan.md"
        assert path.exists()


# ── Task Output ────────────────────────────────────────────────


class TestOutput:
    def test_write_and_read(self):
        storage.write_output("exec-1", "task-1", "Task output here")
        assert storage.read_output("exec-1", "task-1") == "Task output here"

    def test_read_missing_returns_none(self):
        assert storage.read_output("exec-x", "task-x") is None

    def test_has_output_true(self):
        storage.write_output("exec-1", "task-1", "content")
        assert storage.has_output("exec-1", "task-1") is True

    def test_has_output_false(self):
        assert storage.has_output("exec-x", "task-x") is False

    def test_path_structure(self, mock_storage_dir):
        path = storage.output_path("exec-1", "task-1")
        expected = mock_storage_dir / "storage" / "output" / "exec-1" / "task-1.txt"
        assert path == expected

    def test_multiple_tasks_same_execution(self):
        storage.write_output("exec-1", "t1", "Output 1")
        storage.write_output("exec-1", "t2", "Output 2")
        assert storage.read_output("exec-1", "t1") == "Output 1"
        assert storage.read_output("exec-1", "t2") == "Output 2"


# ── Transcripts ────────────────────────────────────────────────


class TestTranscripts:
    def test_write_and_read(self):
        storage.write_transcript("exec-1", "task-1", "Full transcript...")
        assert storage.read_transcript("exec-1", "task-1") == "Full transcript..."

    def test_read_missing_returns_none(self):
        assert storage.read_transcript("exec-x", "task-x") is None

    def test_has_transcript_true(self):
        storage.write_transcript("exec-1", "task-1", "content")
        assert storage.has_transcript("exec-1", "task-1") is True

    def test_has_transcript_false(self):
        assert storage.has_transcript("exec-x", "task-x") is False

    def test_path_structure(self, mock_storage_dir):
        path = storage.transcript_path("exec-1", "task-1")
        expected = (
            mock_storage_dir / "storage" / "transcripts" / "exec-1" / "task-1.txt"
        )
        assert path == expected


# ── Logs ───────────────────────────────────────────────────────


class TestLogs:
    def test_write_and_read(self):
        storage.write_log("exec-1", "Full log content")
        assert storage.read_log("exec-1") == "Full log content"

    def test_read_missing_returns_none(self):
        assert storage.read_log("exec-x") is None

    def test_append_log_single_line(self):
        storage.append_log("exec-1", "Line 1")
        assert storage.read_log("exec-1") == "Line 1\n"

    def test_append_log_multiple_lines(self):
        storage.append_log("exec-1", "Line 1")
        storage.append_log("exec-1", "Line 2")
        storage.append_log("exec-1", "Line 3")
        content = storage.read_log("exec-1")
        assert content == "Line 1\nLine 2\nLine 3\n"

    def test_append_creates_directories(self, mock_storage_dir):
        storage.append_log("new-exec", "first line")
        path = mock_storage_dir / "storage" / "logs" / "new-exec" / "log.txt"
        assert path.exists()

    def test_write_log_overwrites(self):
        storage.write_log("exec-1", "Original")
        storage.write_log("exec-1", "Replaced")
        assert storage.read_log("exec-1") == "Replaced"

    def test_path_structure(self, mock_storage_dir):
        path = storage.log_path("exec-1")
        expected = mock_storage_dir / "storage" / "logs" / "exec-1" / "log.txt"
        assert path == expected

    def test_write_returns_path(self, mock_storage_dir):
        path = storage.write_log("exec-1", "content")
        assert isinstance(path, Path)
        assert path.exists()

    def test_append_returns_path(self):
        path = storage.append_log("exec-1", "line")
        assert isinstance(path, Path)
