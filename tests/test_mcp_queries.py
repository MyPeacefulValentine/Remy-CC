"""Tests for index_mcp_queries.py — MCP query implementations."""
import os
import sys
import sqlite3
import tempfile
import inspect
import json
from pathlib import Path
import pytest

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _REMY_ROOT)
sys.path.insert(0, os.path.join(_REMY_ROOT, "remy-src"))
sys.path.insert(0, os.path.join(_REMY_ROOT, "skills", "remy-index"))

from struct_scan import SCHEMA_SQL
import retrieval_projection


@pytest.fixture
def db_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('a.py','h1','python','Core','[\"b.py\"]')")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('b.py','h2','python','Util',NULL)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('a.py','main','main','function','args',1,10,NULL,NULL,'main')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('a.py','helper','helper','function','x',12,20,NULL,NULL,'helper')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('b.py','process','process','function','data',1,15,NULL,NULL,'process')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('b.py','Util.run','run','function','',17,25,NULL,NULL,'Util run')")
    _now = "2025-01-01T00:00:00"
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('symbol','a.py::main',1,'{\"short\":\"entry point\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('symbol','a.py::helper',1,'{\"short\":\"does stuff\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('symbol','b.py::process',1,'{\"short\":\"processes data\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','main','process','b.py','b.py::process',5,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','main','helper',NULL,'a.py::helper',3,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','helper','run','b.py','b.py::Util.run',14,'inferred',NULL,'interface-impl')")
    edge_id = db.execute("SELECT id FROM edges WHERE caller='main' AND callee='process'").fetchone()[0]
    db.execute("INSERT INTO edge_candidates VALUES (?,?,?)", (edge_id, "b.py::process", 1))
    db.execute("INSERT INTO edge_candidates VALUES (?,?,?)", (edge_id, "c.py::process", 0))
    db.execute("INSERT INTO patterns VALUES (NULL,'a.py','django_signal_connect','post_save','on_save',8,NULL)")
    db.execute("INSERT INTO patterns VALUES (NULL,'b.py','django_signal_send','post_save',NULL,3,NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('c.py','h3','python','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('d.py','h4','python','Util',NULL)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('c.py','do_thing','do_thing','function','x',1,5,NULL,NULL,'do thing')")
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('file','c.py',1,'{\"short\":\"c module short\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO clusters (id,name,label,entry_symbols,file_count) VALUES (1,'test_cluster','My Cluster','[\"c.py::do_thing\"]',2)")
    db.execute("INSERT INTO clusters (id,name,label,entry_symbols,file_count) VALUES (2,'empty_cluster',NULL,'[]',0)")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'c.py')")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'d.py')")
    retrieval_projection.rebuild_projection(db)
    db.commit()
    db.close()
    return tmp_path


class TestOpenDb:
    def test_returns_connection_when_db_exists(self, db_dir, monkeypatch):
        from index_mcp_queries import _open_db
        db = _open_db()
        assert db is not None
        db.close()

    def test_returns_none_when_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import _open_db
        assert _open_db() is None


class TestResolveSymbol:
    def test_find_by_name(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "main")
        db.close()
        assert len(rows) == 1
        assert rows[0][0] == "a.py"
        assert rows[0][1] == "main"

    def test_find_by_qualified(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "b.py::process")
        db.close()
        assert len(rows) == 1
        assert rows[0][1] == "process"

    def test_find_by_short_name(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "run")
        db.close()
        assert len(rows) == 1
        assert rows[0][1] == "Util.run"

    def test_find_with_file_filter(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "main", file="a.py")
        db.close()
        assert len(rows) == 1

    def test_not_found(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "nonexistent")
        db.close()
        assert len(rows) == 0


class TestQuerySymbolImpl:
    def test_returns_formatted_output(self, db_dir):
        from index_mcp_queries import query_symbol_impl
        result = query_symbol_impl("main", None)
        assert "a.py::main" in result
        assert "[function]" in result
        assert "entry point" in result

    def test_no_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_symbol_impl
        result = query_symbol_impl("main", None)
        assert "not found" in result.lower() or "error" in result.lower()

    def test_no_match_returns_message(self, db_dir):
        from index_mcp_queries import query_symbol_impl
        result = query_symbol_impl("zzz_missing", None)
        assert "No symbols found" in result


class TestQueryCallersImpl:
    def test_finds_direct_callers(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("b.py::process", 2, False, False)
        assert "a.py::main" in result

    def test_depth_clamped_to_max(self, db_dir, monkeypatch):
        monkeypatch.setenv("REMY_MCP_BFS_MAX_DEPTH", "1")
        import importlib
        import index_mcp_queries
        importlib.reload(index_mcp_queries)
        result = index_mcp_queries.query_callers_impl("b.py::process", 99, False, False)
        assert "depth 1" in result or "1 levels" in result

    def test_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("b.py::Util.run", 2, False, True)
        assert "a.py::helper" not in result

    def test_include_ambiguous_finds_candidates(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("c.py::process", 2, True, False)
        assert "a.py::main" in result

    def test_not_found_symbol(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("nonexistent", 2, False, False)
        assert "No symbols found" in result


class TestQueryCalleesImpl:
    def test_finds_direct_callees(self, db_dir):
        from index_mcp_queries import query_callees_impl
        result = query_callees_impl("a.py::main", 2, False, False)
        assert "b.py::process" in result

    def test_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_queries import query_callees_impl
        result = query_callees_impl("a.py::helper", 1, False, True)
        assert "Util.run" not in result

    def test_include_ambiguous_expands_candidates(self, db_dir):
        from index_mcp_queries import query_callees_impl
        result = query_callees_impl("a.py::main", 1, True, False)
        assert "b.py::process" in result


class TestQueryImpactImpl:
    def test_finds_downstream(self, db_dir):
        from index_mcp_queries import query_impact_impl
        result = query_impact_impl(["a.py"], 0, 2, False, False)
        assert "b.py" in result

    def test_file_not_in_index(self, db_dir):
        from index_mcp_queries import query_impact_impl
        result = query_impact_impl(["nonexistent.py"], 2, 2, False, False)
        assert "No indexed files" in result

    def test_no_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_impact_impl
        result = query_impact_impl(["a.py"], 2, 2, False, False)
        assert "error" in result.lower() or "not found" in result.lower()


class TestQueryPatternsImpl:
    def test_find_by_type(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl("django_signal_connect", None, None)
        assert "post_save" in result
        assert "on_save" in result

    def test_find_by_signal_name(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl(None, "post_save", None)
        assert "django_signal" in result

    def test_find_by_file(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl(None, None, "a.py")
        assert "on_save" in result
        assert "1 results" in result

    def test_no_match(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl("nonexistent_type", None, None)
        assert "No patterns found" in result


class TestQuerySymbolSummaryImpl:
    def test_returns_summary_text(self, db_dir):
        from index_mcp_queries import query_symbol_summary_impl
        result = query_symbol_summary_impl("process", None)
        assert "processes data" in result

    def test_no_summary_shows_placeholder(self, db_dir):
        from index_mcp_queries import query_symbol_summary_impl
        result = query_symbol_summary_impl("run", None)
        assert "no summary available" in result


class TestQueryFileSummaryImpl:
    def test_returns_file_metadata_with_placeholder(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("a.py")
        assert "## a.py" in result
        assert "2 symbols" in result
        assert "layer=Core" in result
        assert "no summary available" in result

    def test_unknown_path_returns_error(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("nonexistent.py")
        assert "No file" in result
        assert "nonexistent.py" in result

    def test_empty_path_returns_error(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("")
        assert result.startswith("Error:")

    def test_normalizes_backslash_in_path(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("dir\\sub\\nonexistent.py")
        assert "dir/sub/nonexistent.py" in result


class TestQueryClusterFilesImpl:
    def test_empty_cluster_name_returns_error(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("")
        assert result.startswith("Error:")

    def test_unknown_cluster_returns_error(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("nonexistent_cluster")
        assert "No cluster" in result
        assert "nonexistent_cluster" in result

    def test_cluster_with_no_members_returns_message(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("empty_cluster")
        assert "no member files" in result

    def test_lists_files_with_layer(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        assert "## test_cluster" in result
        assert "2 files" in result
        assert "c.py" in result
        assert "d.py" in result
        assert "layer=Core" in result
        assert "layer=Util" in result

    def test_alias_shown_in_header(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        assert "[alias: My Cluster]" in result

    def test_with_summary_includes_short(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster", with_summary=True)
        assert "c module short" in result

    def test_with_summary_placeholder_when_missing(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster", with_summary=True)
        assert "(no summary available)" in result

    def test_alphabetical_ordering(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        c_idx = result.index("c.py")
        d_idx = result.index("d.py")
        assert c_idx < d_idx


class TestBfsChunking:
    """Verify ambiguous BFS works when current set exceeds chunk size (400)."""

    @pytest.fixture
    def large_db_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.executescript(SCHEMA_SQL)

        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('target.py','h0','python','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('target.py','target_fn','target_fn','function',NULL,1,10,NULL,NULL,'target fn')")

        for i in range(500):
            fname = f"f{i:04d}.py"
            db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,'python','Core','[]')", (fname, f"h{i}"))
            db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES (?,?,?,'function',NULL,1,5,NULL,NULL,?)", (fname, f"caller_{i}", f"caller_{i}", f"caller {i}"))
            db.execute(
                "INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,line) VALUES (?,?,?,?,?,?)",
                (fname, f"caller_{i}", "target_fn", "target.py", "target.py::target_fn", 1)
            )

        db.commit()
        db.close()
        return tmp_path

    def test_ambiguous_callers_over_400(self, large_db_dir):
        from index_mcp_queries import _bfs_callers_ambiguous, _open_db
        db = _open_db()
        assert db is not None
        try:
            levels = _bfs_callers_ambiguous(db, {"target.py::target_fn"}, 1)
            assert 1 in levels
            assert len(levels[1]) == 500
        finally:
            db.close()

    def test_ambiguous_callees_over_400(self, large_db_dir):
        from index_mcp_queries import _bfs_callees_ambiguous, _open_db
        db = _open_db()
        assert db is not None
        try:
            all_callers = {f"f{i:04d}.py::caller_{i}" for i in range(500)}
            levels = _bfs_callees_ambiguous(db, all_callers, 1)
            assert 1 in levels
            assert "target.py::target_fn" in levels[1]
        finally:
            db.close()


class TestQuerySearch:
    def test_fts_prefix_match(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc", limit=5)
        assert "process" in result
        assert "FTS5" in result

    def test_fts_exact_name_ranked_first(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=5)
        lines = [l for l in result.splitlines() if "a.py::main" in l]
        assert len(lines) == 1

    def test_like_fallback_on_prefix(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("hel", limit=5)
        assert "helper" in result
        assert "LIKE" in result

    def test_fuzzy_fallback_on_typo(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("processs", limit=5)
        assert "process" in result
        assert "fuzzy" in result

    def test_file_hint_filters(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=5, file_hint="b.py")
        assert "a.py" not in result

    def test_no_match_returns_message(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("zzzznonexistent", limit=5)
        assert "No symbols found" in result

    def test_fts_not_available(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL, language TEXT, layer TEXT DEFAULT 'Core', imports TEXT)")
        db.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_path TEXT, name TEXT, short_name TEXT, type TEXT, args TEXT, lineno INTEGER, end_lineno INTEGER, hash TEXT, summary TEXT, bases TEXT, name_tokens TEXT DEFAULT '')")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.commit()
        db.close()
        from index_mcp_queries import query_search_impl
        result = query_search_impl("test", limit=5)
        assert "FTS index not available" in result

    def test_limit_respected(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=1)
        content_lines = [l for l in result.splitlines() if l.strip().startswith("[")]
        assert len(content_lines) <= 1

    def test_exact_name_ranks_above_prefix(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("helper", limit=5)
        lines = [l.strip() for l in result.splitlines() if l.strip().startswith("[")]
        assert len(lines) >= 1
        assert "helper" in lines[0]

    def test_multiterm_fts_query(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.executescript(SCHEMA_SQL)
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('m.py','h1','python','Core',NULL)")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('m.py','getUserById','getUserById','function',NULL,1,5,NULL,NULL,'get User By Id')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('m.py','getItem','getItem','function',NULL,7,10,NULL,NULL,'get Item')")
        db.commit()
        db.close()
        from index_mcp_queries import query_search_impl
        result = query_search_impl("get User", limit=5)
        assert "getUserById" in result

    def test_empty_string_input(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("", limit=5)
        assert isinstance(result, str)

    def test_special_characters_input(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc*", limit=5)
        assert isinstance(result, str)
        assert "error" not in result.lower() or "No symbols" in result


class TestRetrievalBaseline:
    @staticmethod
    def _module():
        from eval import retrieval_baseline
        return retrieval_baseline

    def test_declared_tasks_cover_required_scenarios(self):
        baseline = self._module()
        spec_path = (Path(__file__).resolve().parents[1] / "eval" / "tasks" /
                     "retrieval_baseline" / "p1_1.json")
        spec = baseline.load_spec(spec_path)
        scenarios = {task["scenario"] for task in spec["tasks"]}
        assert scenarios == {
            "exact qualified name",
            "exact short name",
            "name prefix",
            "snake_case tokenization",
            "camelCase tokenization",
            "C++ namespace tokenization",
            "summary BM25",
            "summary versus name conflict",
            "multi-term implicit AND",
            "LIKE substring fallback",
            "fuzzy typo fallback",
            "file_hint positive filter",
            "file_hint removes all candidates",
            "empty query records current LIKE wildcard behavior",
            "special characters and FTS escaping",
            "no result",
        }

    def test_declared_ground_truth_is_validated(self, tmp_path):
        baseline = self._module()
        spec = {
            "format_version": "1.0.0",
            "fixture": {
                "symbols": [{"file_path": "a.py", "name": "known"}],
            },
            "tasks": [{
                "id": "invalid",
                "query": "missing",
                "expected_nodes": ["a.py::missing"],
                "expected_empty": False,
            }],
        }
        path = tmp_path / "invalid.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        with pytest.raises(ValueError, match="outside the fixture"):
            baseline.load_spec(path)

    def test_channels_and_public_fallback_are_recorded(self):
        baseline = self._module()
        spec_path = (Path(__file__).resolve().parents[1] / "eval" / "tasks" /
                     "retrieval_baseline" / "p1_1.json")
        result = baseline.run_baseline(
            baseline.load_spec(spec_path), warmups=0, iterations=1
        )
        assert len(result["tasks"]) == 16
        assert all("channel_status" in task for task in result["tasks"])
        assert all(
            set(task["channels"]) == {"fts", "like", "fuzzy"}
            for task in result["tasks"]
        )
        assert all("public_output" in task for task in result["tasks"])
        assert result["metrics"]["expected_error_task_count"] == 0

    def test_p1_2_spec_records_errors_filters_and_diagnostics(self):
        baseline = self._module()
        spec_path = (Path(__file__).resolve().parents[1] / "eval" / "tasks" /
                     "retrieval_baseline" / "p1_2.json")
        result = baseline.run_baseline(
            baseline.load_spec(spec_path), warmups=0, iterations=1
        )
        assert result["format_version"] == "1.1.0"
        assert result["metrics"]["expected_error_accuracy"] == 1.0
        assert all(task["error_matches_expectation"] for task in result["tasks"])
        any_task = next(task for task in result["tasks"] if task["id"] == "summary_bm25_any")
        assert any_task["channel_diagnostics"]["fts"]["candidate_cap"] == 50
        assert "per_term" in any_task["channel_diagnostics"]["fts"]

    def test_compare_results_classifies_empty_query_contract_change(self):
        baseline = self._module()
        old = {
            "meta": {"spec_id": "old"},
            "tasks": [{
                "id": "empty_query", "actual_channel": "like",
                "selected_candidates": [{"node_ref": "a.py::main"}],
                "public_output": "old",
            }],
            "metrics": {}, "database": {},
        }
        new = {
            "tasks": [{
                "id": "empty_query", "actual_channel": None,
                "actual_error": True, "selected_candidates": [],
                "public_output": "Error: text must not be empty.",
            }],
            "metrics": {}, "database": {},
        }
        comparison = baseline.compare_results(new, old)
        assert comparison["tasks"][0]["classification"] == "intentional_contract_change"

    def test_measure_records_three_warmups_and_thirty_samples(self):
        baseline = self._module()
        calls = {"count": 0}

        def probe():
            calls["count"] += 1

        timing = baseline.measure(probe, warmups=3, iterations=30)
        assert calls["count"] == 33
        assert len(timing["samples"]) == 30
        assert timing["min"] <= timing["p50"] <= timing["p95"] <= timing["max"]

    def test_rank_metrics_and_empty_tasks_are_separate(self):
        baseline = self._module()
        candidates = [
            {"node_ref": "a.py::first"},
            {"node_ref": "a.py::target"},
        ]
        score = baseline.score_ranked(candidates, ["a.py::target"])
        assert score["recall_at_1"] == 0.0
        assert score["recall_at_5"] == 1.0
        assert score["reciprocal_rank"] == 0.5
        empty = baseline.score_ranked([], [])
        assert empty["eligible"] is False
        assert empty["reciprocal_rank"] is None

    def test_query_search_signature_and_tool_names_remain_stable(self):
        import asyncio
        import index_mcp_server
        import index_mcp_queries

        signature = inspect.signature(index_mcp_queries.query_search_impl)
        assert list(signature.parameters) == [
            "text", "limit", "file_hint", "match", "language",
            "symbol_type", "path_hint",
        ]
        assert signature.parameters["limit"].default == 10
        assert signature.parameters["file_hint"].default == ""
        assert signature.parameters["match"].default == "all"
        assert signature.parameters["match"].kind == inspect.Parameter.KEYWORD_ONLY
        names = {tool.name for tool in asyncio.run(index_mcp_server.mcp.list_tools())}
        assert names == {
            "query_symbol", "query_symbol_summary", "query_file_summary",
            "query_callers", "query_callees", "query_impact", "query_patterns",
            "query_search", "query_flow", "query_cluster_summary",
            "query_cluster_files", "query_navigate",
        }

    @pytest.mark.parametrize("text", ["", "   ", "*** (:)"])
    def test_invalid_query_returns_error(self, db_dir, text):
        from index_mcp_queries import query_search_impl

        assert query_search_impl(text).startswith("Error:")

    @pytest.mark.parametrize("field,value", [
        ("match", "invalid"),
        ("language", "rust"),
        ("language", "   "),
        ("symbol_type", "method"),
        ("symbol_type", "   "),
    ])
    def test_invalid_enum_returns_error(self, db_dir, field, value):
        from index_mcp_queries import query_search_impl

        assert query_search_impl("target", **{field: value}).startswith("Error:")

    def test_path_alias_conflict_returns_error(self, db_dir):
        from index_mcp_queries import query_search_impl

        result = query_search_impl(
            "target", file_hint="src/", path_hint="tests/"
        )
        assert result.startswith("Error:")

    def test_path_nul_returns_error(self, db_dir):
        from index_mcp_queries import query_search_impl

        result = query_search_impl("target", path_hint="src/\0file")
        assert result.startswith("Error:")
        assert "NUL" in result

    def test_path_alias_equivalence_and_normalization(self, db_dir):
        from index_mcp_queries import query_search_impl

        result = query_search_impl(
            "main", file_hint="A.PY", path_hint="a.py"
        )
        assert "a.py::main" in result

    def test_language_and_type_filters(self, db_dir):
        from index_mcp_queries import query_search_impl

        result = query_search_impl(
            "entry", language="python", symbol_type="function"
        )
        assert "a.py::main" in result
        assert query_search_impl("entry", language="c_cpp").startswith(
            "No symbols found"
        )

    def test_match_modes(self, db_dir):
        from index_mcp_queries import query_search_impl

        assert "a.py::main" in query_search_impl("entry point", match="all")
        any_result = query_search_impl("entry data", match="any")
        assert "a.py::main" in any_result
        assert "b.py::process" in any_result
        assert "a.py::main" in query_search_impl("entry point", match="phrase")
        assert query_search_impl("point entry", match="phrase").startswith(
            "No symbols found"
        )

    def test_fuzzy_same_name_respects_final_limit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db = sqlite3.connect(str(claude_dir / "logic_index.db"))
        db.executescript(SCHEMA_SQL)
        for index, path in enumerate(("b.py", "a.py")):
            db.execute(
                "INSERT INTO files (path, struct_hash, language) VALUES (?,?,?)",
                (path, f"h{index}", "PythonParser"),
            )
            db.execute(
                "INSERT INTO symbols (file_path,name,short_name,type,lineno,name_tokens) "
                "VALUES (?,?,?,?,?,?)",
                (path, "duplicateHandler", "duplicateHandler", "function",
                 index + 1, "duplicate Handler"),
            )
        retrieval_projection.rebuild_projection(db)
        db.commit()
        db.close()

        from index_mcp_queries import query_search_impl
        result = query_search_impl("duplicateHandlr", limit=1)
        lines = [line for line in result.splitlines() if line.strip().startswith("[")]
        assert len(lines) == 1
        assert "a.py::duplicateHandler" in lines[0]

    @pytest.mark.parametrize("failing_channel", ["fts", "like", "fuzzy"])
    def test_channel_sqlite_error_stops_fallback(self, db_dir, monkeypatch,
                                                 failing_channel):
        import index_mcp_queries

        calls = []

        def no_match(_db, _query):
            calls.append("no_match")
            return []

        def fail(_db, _query):
            calls.append(failing_channel)
            raise sqlite3.OperationalError("private database detail")

        monkeypatch.setattr(index_mcp_queries, "_search_fts", no_match)
        monkeypatch.setattr(index_mcp_queries, "_search_like", no_match)
        monkeypatch.setattr(index_mcp_queries, "_search_fuzzy", no_match)
        monkeypatch.setattr(index_mcp_queries, f"_search_{failing_channel}", fail)

        result = index_mcp_queries.query_search_impl("missing")
        assert result == (
            f"Error: {failing_channel.upper() if failing_channel != 'fuzzy' else 'fuzzy'} "
            "search failed (OperationalError)."
        )
        assert "private database detail" not in result
        assert calls[-1] == failing_channel
        expected_calls = {"fts": 1, "like": 2, "fuzzy": 3}[failing_channel]
        assert len(calls) == expected_calls

    def test_like_and_fuzzy_order_ignore_insertion_order(self):
        from index_mcp_queries import (
            _make_search_query, _search_fuzzy, _search_like,
        )

        def search(order):
            db = sqlite3.connect(":memory:")
            db.executescript(SCHEMA_SQL)
            for index, path in enumerate(order):
                db.execute(
                    "INSERT INTO files (path, struct_hash, language) VALUES (?,?,?)",
                    (path, f"h{index}", "PythonParser"),
                )
                db.execute(
                    "INSERT INTO symbols "
                    "(file_path,name,short_name,type,lineno,name_tokens) "
                    "VALUES (?,?,?,?,?,?)",
                    (path, "stableHandler", "stableHandler", "function",
                     index + 1, "stable Handler"),
                )
            query = _make_search_query("stableHan", 10)
            like = [(row[0], row[1]) for row in _search_like(db, query)]
            fuzzy_query = _make_search_query("stableHandlr", 10)
            fuzzy = [(row[0], row[1]) for row in _search_fuzzy(db, fuzzy_query)]
            db.close()
            return like, fuzzy

        assert search(("b.py", "a.py")) == search(("a.py", "b.py"))


@pytest.fixture
def flow_db_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('src/fs/read.c','h1','c','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('src/fs/write.c','h2','c','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('src/fs/vfs.c','h3','c','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('models/resnet.py','h4','python','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('models/vgg.py','h5','python','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('train.py','h6','python','Util',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('losses.py','h7','python','Util',NULL)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/read.c','sys_read','sys_read','function','fd,buf,count',10,50,NULL,NULL,'sys read')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/read.c','vfs_read','vfs_read','function','file,buf,count',55,100,NULL,NULL,'vfs read')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/vfs.c','new_sync_read','new_sync_read','function','filp,buf',5,30,NULL,NULL,'new sync read')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/write.c','sys_write','sys_write','function','fd,buf,count',10,50,NULL,NULL,'sys write')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('models/resnet.py','ResNet.forward','forward','function','self,x',20,40,NULL,NULL,'Res Net forward')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('models/vgg.py','VGG.forward','forward','function','self,x',15,35,NULL,NULL,'VGG forward')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('train.py','train_epoch','train_epoch','function','model,loader',5,50,NULL,NULL,'train epoch')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('losses.py','compute_loss','compute_loss','function','pred,target',3,20,NULL,NULL,'compute loss')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/read.c','sys_read','vfs_read','src/fs/read.c','src/fs/read.c::vfs_read',15,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/read.c','vfs_read','new_sync_read','src/fs/vfs.c','src/fs/vfs.c::new_sync_read',60,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'train.py','train_epoch','forward','models/resnet.py','models/resnet.py::ResNet.forward',25,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'models/resnet.py','ResNet.forward','compute_loss',NULL,'losses.py::compute_loss',35,'inferred',NULL,'interface-impl')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/vfs.c','new_sync_read','sys_write',NULL,NULL,20,NULL,NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/vfs.c','orphan_caller','sys_read','src/fs/read.c','src/fs/read.c::sys_read',99,'definite',NULL,NULL)")
    db.commit()
    db.close()
    return tmp_path


class TestLoadGraph:
    def test_loads_edges_into_adjacency_lists(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, id_to_info, skipped = _load_graph(db)
        db.close()
        sys_read_id = name_to_id.get("src/fs/read.c::sys_read")
        vfs_read_id = name_to_id.get("src/fs/read.c::vfs_read")
        assert sys_read_id is not None
        assert vfs_read_id is not None
        targets = [t for t, _, _ in adj_fwd[sys_read_id]]
        assert vfs_read_id in targets

    def test_skips_orphan_source_edges(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        _, _, _, _, skipped = _load_graph(db)
        db.close()
        assert skipped >= 1

    def test_static_only_excludes_heuristic(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        adj_fwd_all, _, _, _, _ = _load_graph(db, static_only=False)
        adj_fwd_static, _, _, _, _ = _load_graph(db, static_only=True)
        db.close()
        total_all = sum(len(v) for v in adj_fwd_all.values())
        total_static = sum(len(v) for v in adj_fwd_static.values())
        assert total_static < total_all

    def test_id_to_info_contains_all_symbols(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        _, _, _, id_to_info, _ = _load_graph(db)
        db.close()
        assert len(id_to_info) == 8

    def test_bidirectional_consistency(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        adj_fwd, adj_bwd, _, _, _ = _load_graph(db)
        db.close()
        for src, edges in adj_fwd.items():
            for tgt, prov, via in edges:
                back_sources = [s for s, _, _ in adj_bwd.get(tgt, [])]
                assert src in back_sources


class TestBidirBfs:
    def test_finds_direct_connection(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/read.c::vfs_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        assert len(path) == 2
        assert path[0][0] == src
        assert path[1][0] == tgt

    def test_finds_two_hop_path(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        assert len(path) == 3

    def test_same_node_returns_single_element(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        nid = name_to_id["src/fs/read.c::sys_read"]
        path = _bidir_bfs(nid, nid, adj_fwd, adj_bwd, 15, 2000)
        assert path == [(nid, None, None)]

    def test_disconnected_returns_none(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["train.py::train_epoch"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is None

    def test_max_visited_caps_expansion(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 1)
        assert path is None

    def test_path_edges_are_valid(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        for i in range(len(path) - 1):
            cur_id = path[i][0]
            nxt_id = path[i + 1][0]
            targets = [t for t, _, _ in adj_fwd.get(cur_id, [])]
            assert nxt_id in targets

    def test_symmetry_both_directions_find_path(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        a = name_to_id["train.py::train_epoch"]
        b = name_to_id["models/resnet.py::ResNet.forward"]
        path_ab = _bidir_bfs(a, b, adj_fwd, adj_bwd, 15, 2000)
        path_ba = _bidir_bfs(b, a, adj_fwd, adj_bwd, 15, 2000)
        assert path_ab is not None
        assert path_ba is None


class TestResolveFlowSymbol:
    def test_file_qualified_syntax(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "src/fs/read.c:sys_read", db, name_to_id, adj_fwd, adj_bwd, set(), ["sys_read"]
        )
        db.close()
        assert sid is not None
        assert "sys_read" in qualified
        assert ambiguous is False

    def test_class_qualified_dot_syntax(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "ResNet.forward", db, name_to_id, adj_fwd, adj_bwd, set(), ["ResNet", "forward"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()

    def test_bare_name_unique_resolves(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "sys_read", db, name_to_id, adj_fwd, adj_bwd, set(), ["sys_read"]
        )
        db.close()
        assert sid is not None
        assert ambiguous is False

    def test_bare_name_ambiguous_uses_co_naming(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "forward", db, name_to_id, adj_fwd, adj_bwd, set(), ["ResNet", "forward", "compute_loss"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()
        assert ambiguous is False

    def test_bare_name_ambiguous_connectivity(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        train_id = name_to_id["train.py::train_epoch"]
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "forward", db, name_to_id, adj_fwd, adj_bwd, {train_id}, ["forward"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()

    def test_not_found_returns_none(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "nonexistent_func", db, name_to_id, adj_fwd, adj_bwd, set(), ["nonexistent_func"]
        )
        db.close()
        assert sid is None

    def test_connectivity_prefers_closer_candidate(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        resnet_id = name_to_id["models/resnet.py::ResNet.forward"]
        loss_id = name_to_id["losses.py::compute_loss"]
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "forward", db, name_to_id, adj_fwd, adj_bwd, {loss_id}, ["forward"]
        )
        db.close()
        assert sid == resnet_id
        assert "resnet" in qualified.lower()


class TestQueryFlowImpl:
    def test_linear_chain_connected(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read", "vfs_read", "new_sync_read"])
        assert "## Flow" in result
        assert "sys_read" in result
        assert "vfs_read" in result
        assert "new_sync_read" in result
        assert "↓ call" in result

    def test_partial_connectivity_shows_break(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read", "train_epoch"])
        assert "Break" in result or "No connected" in result

    def test_synthesized_edge_annotated(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["train_epoch", "ResNet.forward", "compute_loss"])
        assert "synthesized" in result
        assert "interface-impl" in result

    def test_static_only_excludes_synthesized(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["ResNet.forward", "compute_loss"], static_only=True)
        assert "Break" in result or "No connected" in result

    def test_less_than_two_symbols_error(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read"])
        assert "Error" in result
        assert "at least 2" in result

    def test_empty_list_error(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl([])
        assert "Error" in result

    def test_no_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["a", "b"])
        assert "Error" in result or "logic_index.db" in result

    def test_all_unresolved_returns_message(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["xxx_not_exist", "yyy_not_exist"])
        assert "No symbols resolved" in result

    def test_file_qualified_input(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["src/fs/read.c:sys_read", "src/fs/read.c:vfs_read"])
        assert "## Flow" in result
        assert "sys_read" in result

    def test_max_depth_respected(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read", "new_sync_read"], max_depth=0)
        assert "Break" in result or "No connected" in result

    def test_flow_parameters_clamped_to_config(self, flow_db_dir, monkeypatch):
        import index_mcp_queries
        monkeypatch.setenv("REMY_FLOW_MAX_DEPTH", "1")
        monkeypatch.setenv("REMY_FLOW_MAX_VISITED", "100")
        observed = {}
        original = index_mcp_queries._bidir_bfs

        def capture(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited):
            observed["limits"] = (max_depth, max_visited)
            return original(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited)

        monkeypatch.setattr(index_mcp_queries, "_bidir_bfs", capture)
        index_mcp_queries.query_flow_impl(
            ["sys_read", "new_sync_read"], max_depth=20, max_visited=5000
        )
        assert observed["limits"] == (1, 100)

    def test_query_uses_one_config_snapshot(self, flow_db_dir, monkeypatch):
        import index_mcp_queries
        calls = 0
        original = index_mcp_queries.remy_config.load_config

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(index_mcp_queries.remy_config, "load_config", counted)
        index_mcp_queries.query_flow_impl(["sys_read", "new_sync_read"])
        assert calls == 1

    def test_format_flow_probable_label(self, flow_db_dir):
        from index_mcp_queries import _format_flow
        id_to_info = {
            1: ("a.py::foo", "a.py", "foo", 10, "function"),
            2: ("b.py::bar", "b.py", "bar", 20, "function"),
        }
        resolved = [(1, "a.py::foo", False), (2, "b.py::bar", False)]
        segments = [[(1, None, None), (2, "probable", None)]]
        result = _format_flow(resolved, segments, id_to_info, False, 15)
        assert "call [name-match]" in result

    def test_format_flow_speculative_label(self, flow_db_dir):
        from index_mcp_queries import _format_flow
        id_to_info = {
            1: ("a.py::foo", "a.py", "foo", 10, "function"),
            2: ("b.py::bar", "b.py", "bar", 20, "function"),
        }
        resolved = [(1, "a.py::foo", False), (2, "b.py::bar", False)]
        segments = [[(1, None, None), (2, "speculative", None)]]
        result = _format_flow(resolved, segments, id_to_info, False, 15)
        assert "call [speculative resolution]" in result


class TestSearchFtsNodeKindFilter:
    """_search_fts must return symbol-layer rows only (P1-8)."""

    def test_only_symbol_layer_returned(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        _now = "2025-01-01T00:00:00"
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('file', 'a.py', 1, "
            "'{\"short\":\"entry shared keyword\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('cluster', 'mycluster', 1, "
            "'{\"short\":\"entry shared keyword\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "entry", limit=10, file_hint="")
        db.close()
        for r in results:
            name, fpath, lineno, stype, _rank = r
            assert name is not None
            assert fpath in ("a.py", "b.py")

    def test_zero_results_when_only_non_symbol_match(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('cluster', 'mycluster', 1, "
            "'{\"short\":\"uniqueclusterkeyword\",\"full\":null}', 'ok', "
            "'2025-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "uniqueclusterkeyword", limit=10, file_hint="")
        db.close()
        assert results == []

    def test_file_layer_matches_excluded(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('file', 'a.py', 1, "
            "'{\"short\":\"uniquefilekeyword\",\"full\":null}', 'ok', "
            "'2025-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "uniquefilekeyword", limit=10, file_hint="")
        db.close()
        assert results == []

    def test_symbol_match_still_returned_alongside_non_symbol(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        _now = "2025-01-01T00:00:00"
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'a.py::main', 2, "
            "'{\"short\":\"shared keyword indicator\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        retrieval_projection.refresh_node(conn, "symbol", "a.py::main")
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('cluster', 'mycluster', 1, "
            "'{\"short\":\"shared keyword indicator\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "indicator", limit=10, file_hint="")
        db.close()
        assert len(results) >= 1
        for r in results:
            name, fpath, _lineno, _stype, _rank = r
            assert fpath in ("a.py", "b.py")
