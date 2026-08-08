"""Tests for index_mcp_common.py — shared DB access for MCP queries."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))


class TestOpenDb:
    def test_returns_connection_when_db_exists(self, db_dir, monkeypatch):
        from index_mcp_common import _open_db
        db = _open_db()
        assert db is not None
        db.close()

    def test_returns_none_when_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_common import _open_db
        assert _open_db() is None
