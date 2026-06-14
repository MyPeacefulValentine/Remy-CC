"""Tests for index_mcp_server freshness detection (D.2)."""
import hashlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))


@pytest.fixture
def freshness_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('last_updated', '2026-06-15T00:00:00')")
    db.execute("INSERT INTO meta VALUES ('file_count', '10')")
    db.execute("INSERT INTO meta VALUES ('source_commit', 'aabbccdd11223344')")
    src = tmp_path / "a.py"
    src.write_text("def foo(): pass\n", encoding="utf-8")
    content = src.read_text(encoding="utf-8")
    h = hashlib.md5(content.encode("utf-8")).hexdigest()
    db.execute("INSERT INTO files VALUES (?, ?)", ("a.py", h))
    db.commit()
    db.close()
    return tmp_path


class TestInitFreshness:
    def _reset(self):
        import index_mcp_server
        index_mcp_server._freshness_warning = ""

    def test_no_db_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._reset()
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert index_mcp_server._freshness_warning == ""

    def test_git_same_commit_no_dirty_returns_fresh(self, freshness_db, monkeypatch):
        self._reset()
        import subprocess as sp
        def mock_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                return sp.CompletedProcess(cmd, 0, stdout="aabbccdd11223344\n", stderr="")
            if "status" in cmd:
                return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
            return sp.CompletedProcess(cmd, 1, stdout="", stderr="")
        monkeypatch.setattr("index_mcp_server.subprocess.run", mock_run)
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert index_mcp_server._freshness_warning == ""

    def test_git_same_commit_high_dirty_returns_warning(self, freshness_db, monkeypatch):
        self._reset()
        import subprocess as sp
        dirty_lines = "\n".join([f" M file{i}.py" for i in range(6)])
        def mock_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                return sp.CompletedProcess(cmd, 0, stdout="aabbccdd11223344\n", stderr="")
            if "status" in cmd:
                return sp.CompletedProcess(cmd, 0, stdout=dirty_lines, stderr="")
            return sp.CompletedProcess(cmd, 1, stdout="", stderr="")
        monkeypatch.setattr("index_mcp_server.subprocess.run", mock_run)
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert "Warning" in index_mcp_server._freshness_warning
        assert "6 files" in index_mcp_server._freshness_warning

    def test_git_different_commit_returns_warning(self, freshness_db, monkeypatch):
        self._reset()
        import subprocess as sp
        def mock_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                return sp.CompletedProcess(cmd, 0, stdout="deadbeef00000000\n", stderr="")
            return sp.CompletedProcess(cmd, 1, stdout="", stderr="")
        monkeypatch.setattr("index_mcp_server.subprocess.run", mock_run)
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert "Warning" in index_mcp_server._freshness_warning
        assert "aabbccdd" in index_mcp_server._freshness_warning
        assert "deadbeef" in index_mcp_server._freshness_warning

    def test_git_unavailable_falls_to_hash(self, freshness_db, monkeypatch):
        self._reset()
        def mock_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")
        monkeypatch.setattr("index_mcp_server.subprocess.run", mock_run)
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert index_mcp_server._freshness_warning == ""

    def test_hash_mismatch_above_threshold_returns_warning(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._reset()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db = sqlite3.connect(str(claude_dir / "logic_index.db"))
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('file_count', '5')")
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text(f"x={i}\n", encoding="utf-8")
            db.execute(f"INSERT INTO files VALUES ('f{i}.py', 'wrong_hash_{i}')")
        db.commit()
        db.close()
        def mock_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")
        monkeypatch.setattr("index_mcp_server.subprocess.run", mock_run)
        monkeypatch.setattr("index_mcp_server.random.sample", lambda lst, n: lst[:n])
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert "Warning" in index_mcp_server._freshness_warning

    def test_no_source_commit_falls_to_hash(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._reset()
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db = sqlite3.connect(str(claude_dir / "logic_index.db"))
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('file_count', '1')")
        (tmp_path / "m.py").write_text("x=1\n", encoding="utf-8")
        content = (tmp_path / "m.py").read_text(encoding="utf-8")
        h = hashlib.md5(content.encode("utf-8")).hexdigest()
        db.execute("INSERT INTO files VALUES ('m.py', ?)", (h,))
        db.commit()
        db.close()
        import subprocess as sp
        def mock_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                return sp.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
            return sp.CompletedProcess(cmd, 1, stdout="", stderr="")
        monkeypatch.setattr("index_mcp_server.subprocess.run", mock_run)
        import index_mcp_server
        index_mcp_server._init_freshness()
        assert index_mcp_server._freshness_warning == ""


class TestWithFreshness:
    def test_error_passthrough(self):
        import index_mcp_server
        index_mcp_server._freshness_warning = "[Warning: stale]"
        result = index_mcp_server._with_freshness("Error: DB not found.")
        assert result == "Error: DB not found."

    def test_prepends_warning_when_stale(self):
        import index_mcp_server
        index_mcp_server._freshness_warning = "[Warning: index stale]"
        result = index_mcp_server._with_freshness("## callers of foo")
        assert result.startswith("[Warning: index stale]")
        assert "## callers of foo" in result

    def test_no_prepend_when_fresh(self):
        import index_mcp_server
        index_mcp_server._freshness_warning = ""
        result = index_mcp_server._with_freshness("## callers of foo")
        assert result == "## callers of foo"
