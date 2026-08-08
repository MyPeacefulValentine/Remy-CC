"""Tests for query_navigate / query_cluster_summary MCP queries."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))
from struct_scan import SCHEMA_SQL, VERSION, tokenize_symbol
from retrieval_projection import rebuild_projection
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
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('archive.py', 'h3')")
    conn.execute(
        "INSERT INTO clusters (name, label, entry_symbols, file_count) VALUES ('archive', NULL, '[]', 1)"
    )
    cid_archive = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'archive.py')", (cid_archive,))
    symbol_rows = (
        ("auth.py", "login_handler", "function", "request", 5),
        ("crypto.py", "encrypt_payload", "function", "data", 8),
        ("crypto.py", "encrypt_session", "function", "items", 21),
    )
    for file_path, name, stype, args, lineno in symbol_rows:
        conn.execute(
            "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, "
            "end_lineno, hash, bases, name_tokens) VALUES (?,?,?,?,?,?,?,?,NULL,?)",
            (file_path, name, name, stype, args, lineno, lineno + 10,
             f"h-{name}", tokenize_symbol(name)),
        )
    summarizer.write_summary_version(conn, "cluster", "security",
                                     {"short": "Authentication and crypto utilities",
                                      "full": "[定位] auth+crypto"}, "ok")
    summarizer.write_summary_version(conn, "cluster", "archive",
                                     {"short": "Cold storage archival utilities", "full": None}, "ok")
    summarizer.write_summary_version(conn, "file", "auth.py",
                                     {"short": "Login and session handling", "full": None}, "ok")
    summarizer.write_summary_version(conn, "file", "crypto.py",
                                     {"short": "Symmetric encryption helpers", "full": None}, "ok")
    summarizer.write_summary_version(conn, "file", "archive.py",
                                     {"short": "Rotates archived blobs", "full": None}, "ok")
    summarizer.write_summary_version(conn, "symbol", "auth.py::login_handler",
                                     {"short": "Validates login credentials", "full": None}, "ok")
    rebuild_projection(conn)
    conn.commit()
    conn.close()
    monkeypatch.chdir(tmp_path)
    if "index_mcp_navigate" in sys.modules:
        del sys.modules["index_mcp_navigate"]
    import index_mcp_navigate
    yield index_mcp_navigate


class TestClusterSummary:
    def test_single_cluster(self, env):
        from index_mcp_facts import query_cluster_summary_impl
        out = query_cluster_summary_impl("security")
        assert "security" in out
        assert "Authentication and crypto utilities" in out

    def test_all_clusters(self, env):
        from index_mcp_facts import query_cluster_summary_impl
        out = query_cluster_summary_impl(None)
        assert "security" in out

    def test_missing_returns_message(self, env):
        from index_mcp_facts import query_cluster_summary_impl
        out = query_cluster_summary_impl("nonexistent")
        assert "No clusters found" in out


class TestNavigate:
    def test_empty_intent_errors(self, env):
        out = env.query_navigate_impl("", top_k=5)
        assert "Error" in out

    def test_heuristic_match(self, env, monkeypatch):
        monkeypatch.setattr(env, "_try_default_llm_call", lambda: None)
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
        monkeypatch.delenv("REMY_LLM_API_KEY", raising=False)
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


def _prompt_payload(prompt):
    return json.loads(prompt.split("\n\n", 1)[1])


class TestCandidatePipeline:
    def test_no_llm_outputs_candidates_with_sources(self, env, monkeypatch):
        monkeypatch.setattr(env, "_try_default_llm_call", lambda: None)
        out = env.query_navigate_impl("login credentials", top_k=5)
        assert "source=heuristic" in out
        assert "sources:" in out
        assert "login_handler" in out

    def test_prompt_contains_only_selected_candidates(self, env):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps([])

        env.query_navigate_impl("login credentials", top_k=3, llm_call=llm)
        payload = _prompt_payload(prompts[0])
        assert set(payload) == {"intent", "top_k", "candidates"}
        kinds = {entry["kind"] for entry in payload["candidates"]}
        assert kinds <= {"cluster", "file", "symbol"}
        files_in_prompt = {
            entry["file"] for entry in payload["candidates"]
            if entry["kind"] == "file"
        }
        assert "auth.py" in files_in_prompt
        assert "crypto.py" not in files_in_prompt

    def test_cluster_and_file_quotas_cap_candidates(self, env, monkeypatch):
        monkeypatch.setenv("REMY_NAVIGATE_CANDIDATE_CLUSTERS", "1")
        monkeypatch.setenv("REMY_NAVIGATE_CANDIDATE_FILES", "1")
        monkeypatch.setenv("REMY_NAVIGATE_CANDIDATE_SYMBOLS", "1")
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps([])

        env.query_navigate_impl("utilities session encryption", top_k=5, llm_call=llm)
        counts = {}
        for entry in _prompt_payload(prompts[0])["candidates"]:
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
        assert counts == {"cluster": 1, "file": 1, "symbol": 1}

    def test_punctuation_only_intent_treated_as_lexical_empty(self, env, monkeypatch):
        monkeypatch.setattr(env, "_try_default_llm_call", lambda: None)
        out = env.query_navigate_impl("？！。", top_k=3)
        assert out.startswith("No matches")

    def test_symbol_quota_caps_candidates(self, env, monkeypatch):
        monkeypatch.setenv("REMY_NAVIGATE_CANDIDATE_SYMBOLS", "1")
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps([])

        env.query_navigate_impl("encrypt", top_k=5, llm_call=llm)
        payload = _prompt_payload(prompts[0])
        symbols = [e for e in payload["candidates"] if e["kind"] == "symbol"]
        assert len(symbols) == 1

    def test_structure_backfill_has_no_orphans(self, env):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps([])

        env.query_navigate_impl("login credentials", top_k=5, llm_call=llm)
        for entry in _prompt_payload(prompts[0])["candidates"]:
            assert entry["cluster"]
            if entry["kind"] == "symbol":
                assert entry["file"]

    def test_lexical_empty_falls_back_to_cluster_only(self, env):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps([{
                "cluster": "security", "file": None, "symbol": None,
                "relevance_score": 0.8, "rationale": "cluster match",
            }])

        out = env.query_navigate_impl("数据库迁移", top_k=3, llm_call=llm)
        assert "source=llm-cluster-only" in out
        payload = _prompt_payload(prompts[0])
        assert payload["candidates"]
        assert all(e["kind"] == "cluster" for e in payload["candidates"])

    def test_lexical_empty_without_llm_returns_no_matches(self, env, monkeypatch):
        monkeypatch.setattr(env, "_try_default_llm_call", lambda: None)
        out = env.query_navigate_impl("数据库迁移", top_k=3)
        assert out.startswith("No matches")

    def test_fuzzy_gate_recovers_single_word_typo(self, env, monkeypatch):
        monkeypatch.setattr(env, "_try_default_llm_call", lambda: None)
        out = env.query_navigate_impl("loginhandlr", top_k=3)
        assert "login_handler" in out
        assert "fuzzy#" in out


class TestCacheKeySensitivity:
    @staticmethod
    def _counting_llm(counter):
        def llm(_prompt):
            counter["n"] += 1
            return json.dumps([{
                "cluster": "security", "file": "auth.py", "symbol": None,
                "relevance_score": 0.7, "rationale": "r1",
            }])
        return llm

    def test_unrelated_write_keeps_cache(self, env):
        counter = {"n": 0}
        llm = self._counting_llm(counter)
        env.query_navigate_impl("login credentials", top_k=3, llm_call=llm)
        first = counter["n"]

        db_path = os.path.join(os.getcwd(), ".claude", "logic_index.db")
        conn = sqlite3.connect(db_path)
        summarizer.write_summary_version(conn, "file", "crypto.py",
                                         {"short": "AES block cipher modes", "full": None}, "ok")
        conn.close()

        env.query_navigate_impl("login credentials", top_k=3, llm_call=llm)
        assert counter["n"] == first

    def test_top_k_distinguishes_cache_entries(self, env):
        counter = {"n": 0}
        llm = self._counting_llm(counter)
        env.query_navigate_impl("login credentials", top_k=3, llm_call=llm)
        first = counter["n"]
        env.query_navigate_impl("login credentials", top_k=5, llm_call=llm)
        assert counter["n"] == first + 1
