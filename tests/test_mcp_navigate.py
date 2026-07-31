"""Tests for query_navigate / query_cluster_summary MCP queries."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))
from struct_scan import SCHEMA_SQL, VERSION
import summarizer


@pytest.fixture
def env(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('auth.py', 'h1')")
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('crypto.py', 'h2')")
    conn.execute(
        "INSERT INTO clusters (name, label, entry_symbols, file_count) VALUES ('security', NULL, '[]', 2)"
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'auth.py')", (cid,))
    conn.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'crypto.py')", (cid,))
    summarizer.write_summary_version(conn, "cluster", "security",
                                     {"short": "Authentication and crypto utilities",
                                      "full": "[定位] auth+crypto"}, "ok")
    summarizer.write_summary_version(conn, "file", "auth.py",
                                     {"short": "Login and session handling", "full": None}, "ok")
    summarizer.write_summary_version(conn, "file", "crypto.py",
                                     {"short": "Symmetric encryption helpers", "full": None}, "ok")
    conn.commit()
    conn.close()
    monkeypatch.chdir(tmp_path)
    import importlib
    if "index_mcp_queries" in sys.modules:
        del sys.modules["index_mcp_queries"]
    import index_mcp_queries
    yield index_mcp_queries


class TestClusterSummary:
    def test_single_cluster(self, env):
        out = env.query_cluster_summary_impl("security")
        assert "security" in out
        assert "Authentication and crypto utilities" in out

    def test_all_clusters(self, env):
        out = env.query_cluster_summary_impl(None)
        assert "security" in out

    def test_missing_returns_message(self, env):
        out = env.query_cluster_summary_impl("nonexistent")
        assert "No clusters found" in out


class TestNavigate:
    def test_empty_intent_errors(self, env):
        out = env.query_navigate_impl("", top_k=5)
        assert "Error" in out

    def test_heuristic_match(self, env, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        out = env.query_navigate_impl("authentication login", top_k=3)
        assert "auth" in out.lower() or "security" in out.lower()

    def test_llm_call_path(self, env):
        ranked = [{
            "cluster": "security",
            "file": "auth.py",
            "symbol": None,
            "relevance_score": 0.9,
            "rationale": "matched intent",
        }]

        def llm(_prompt):
            return json.dumps(ranked)

        out = env.query_navigate_impl("authentication", top_k=3, llm_call=llm)
        assert "security" in out
        assert "0.9" in out or "0.90" in out

    def test_invalid_llm_response_falls_back(self, env, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        out = env.query_navigate_impl("authentication", top_k=3,
                                      llm_call=lambda _p: "not valid json")
        assert "heuristic" in out or "auth" in out.lower()

    def test_top_k_capped(self, env):
        out = env.query_navigate_impl("auth crypto encryption", top_k=100, llm_call=lambda _p: json.dumps([]))
        assert "heuristic" in out or "matches" in out.lower() or "No matches" in out


class TestCache:
    def test_cache_invalidates_on_new_version(self, env, monkeypatch):
        ranked = [{
            "cluster": "security", "file": None, "symbol": None,
            "relevance_score": 0.7, "rationale": "r1",
        }]
        call_count = {"n": 0}

        def llm(_prompt):
            call_count["n"] += 1
            return json.dumps(ranked)

        env.query_navigate_impl("auth", top_k=3, llm_call=llm)
        first = call_count["n"]

        env.query_navigate_impl("auth", top_k=3, llm_call=llm)
        assert call_count["n"] == first

        db_path = os.path.join(os.getcwd(), ".claude", "logic_index.db")
        conn = sqlite3.connect(db_path)
        summarizer.write_summary_version(conn, "file", "auth.py",
                                         {"short": "v2", "full": None}, "ok")
        conn.close()

        env.query_navigate_impl("auth", top_k=3, llm_call=llm)
        assert call_count["n"] == first + 1
