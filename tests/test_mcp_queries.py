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


def _write_union_db(tmp_path, symbols):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db = sqlite3.connect(str(claude_dir / "logic_index.db"))
    db.executescript(SCHEMA_SQL)
    now = "2025-01-01T00:00:00"
    seen_files = set()
    for index, (path, name, tokens, summary) in enumerate(symbols, 1):
        if path not in seen_files:
            db.execute(
                "INSERT INTO files (path, struct_hash, language) VALUES (?,?,?)",
                (path, f"h{len(seen_files)}", "python"),
            )
            seen_files.add(path)
        db.execute(
            "INSERT INTO symbols (file_path,name,short_name,type,lineno,name_tokens) "
            "VALUES (?,?,?,?,?,?)",
            (path, name, name, "function", index * 10, tokens),
        )
        if summary:
            db.execute(
                "INSERT INTO summary_versions "
                "(node_kind,node_ref,version,summary,status,created_at) "
                "VALUES ('symbol',?,1,?,'ok',?)",
                (f"{path}::{name}",
                 json.dumps({"short": summary, "full": None}), now),
            )
    retrieval_projection.rebuild_projection(db)
    db.commit()
    db.close()


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


class TestQuerySearch:
    def test_union_prefix_match(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc", limit=5)
        assert "process" in result
        assert "matched via union" in result

    def test_fts_exact_name_ranked_first(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=5)
        lines = [l for l in result.splitlines() if "a.py::main" in l]
        assert len(lines) == 1

    def test_union_reports_sources_priority_and_detail(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=5)
        assert "sources: exact#1, prefix#1 | priority=0" in result
        assert "sig: (args) | summary: entry point" in result

    def test_prefix_only_query_matches_via_union(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("hel", limit=5)
        assert "helper" in result
        assert "matched via union" in result
        assert "sources: prefix#1 | priority=1" in result

    def test_fuzzy_fallback_on_typo(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("processs", limit=5)
        assert "process" in result
        assert "matched via fuzzy" in result
        assert "sources: fuzzy#1 | priority=3" in result

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
        assert "sig:" not in result
        assert "summary:" not in result

    def test_empty_string_input(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("", limit=5)
        assert isinstance(result, str)

    def test_special_characters_input(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc*", limit=5)
        assert isinstance(result, str)
        assert "error" not in result.lower() or "No symbols" in result

    def test_exact_channel_respects_language_and_type_filters(self, db_dir):
        from index_mcp_queries import query_search_impl
        assert query_search_impl("main", language="c_cpp").startswith(
            "No symbols found"
        )
        assert query_search_impl("main", symbol_type="class").startswith(
            "No symbols found"
        )
        result = query_search_impl(
            "main", language="python", symbol_type="function"
        )
        assert "a.py::main" in result

    def test_merge_candidates_keeps_first_seen_priority_and_all_sources(self):
        from index_mcp_queries import _merge_candidates

        exact_rows = [("beta", "a.py", 1, "function", 0.0),
                      ("alpha", "a.py", 2, "function", 0.0)]
        prefix_rows = [("alpha", "a.py", 2, "function", 0.0)]
        merged = _merge_candidates(
            [("exact", exact_rows), ("prefix", prefix_rows)], 10
        )
        alpha = next(item for item in merged if item["name"] == "alpha")
        assert alpha["priority"] == 0
        assert alpha["best_rank"] == 2
        assert alpha["sources"] == [("exact", 2), ("prefix", 1)]
        assert [item["name"] for item in merged] == ["beta", "alpha"]
        assert _merge_candidates([("exact", exact_rows)], 1)[0]["name"] == "beta"

    def test_fuzzy_not_called_when_deterministic_candidates_exist(
            self, db_dir, monkeypatch):
        import index_mcp_queries

        def forbidden(_db, _query):
            raise AssertionError("fuzzy must not run with deterministic candidates")

        monkeypatch.setattr(index_mcp_queries, "_search_fuzzy", forbidden)
        result = index_mcp_queries.query_search_impl("helper", limit=5)
        assert "matched via union" in result

    def test_exact_ignores_match_mode(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "AlphaBeta", "Alpha Beta", None),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_search_impl
        result = query_search_impl("alphabeta", limit=5, match="phrase")
        assert "m.py::AlphaBeta" in result
        assert "sources: exact#1 | priority=0" in result

    def test_exact_uses_unicode_casefold(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "Straße", "Straße", None),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_search_impl
        result = query_search_impl("STRASSE", limit=5)
        assert "m.py::Straße" in result
        assert "exact#1" in result

    def test_exact_name_survives_summary_hits(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "process", "process", "other work"),
            ("m.py", "alpha", "alpha", "process everything nightly"),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_search_impl
        result = query_search_impl("process", limit=5)
        assert "m.py::process" in result
        assert "m.py::alpha" in result
        sources_line_at = result.index("sources: exact#1, prefix#1 | priority=0")
        assert result.index("m.py::process") < sources_line_at
        assert sources_line_at < result.index("m.py::alpha")

    def test_priority_prefers_prefix_over_bm25(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("p.py", "proc_one", "proc one", "unrelated"),
            ("p.py", "beta", "beta", "proc handling core"),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc", limit=5)
        assert result.index("p.py::proc_one") < result.index("p.py::beta")

    def test_truncation_applies_after_merge(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "process", "process", "other work"),
            ("m.py", "alpha", "alpha", "process everything nightly"),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_search_impl
        result = query_search_impl("process", limit=1)
        assert "m.py::process" in result
        assert "m.py::alpha" not in result


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
            set(task["channels"]) == {"exact", "prefix", "bm25", "fuzzy"}
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
        assert any_task["channel_diagnostics"]["bm25"]["candidate_cap"] == 50
        assert "per_term" in any_task["channel_diagnostics"]["bm25"]

    def test_p1_3_union_spec_and_name_conflict_acceptance(self):
        baseline = self._module()
        root = (Path(__file__).resolve().parents[1] / "eval" / "tasks" /
                "retrieval_baseline")
        p1_1 = baseline.load_spec(root / "p1_1.json")
        p1_3 = baseline.load_spec(root / "p1_3.json")
        assert p1_3["format_version"] == "1.1.0"
        assert ({task["id"] for task in p1_3["tasks"]}
                == {task["id"] for task in p1_1["tasks"]})
        assert all(task.get("expected_channel") in ("union", "fuzzy", None)
                   for task in p1_3["tasks"])

        result = baseline.run_baseline(p1_3, warmups=0, iterations=1)
        assert result["metrics"]["expected_error_accuracy"] == 1.0
        assert result["metrics"]["recall_at_5"] == 1.0
        assert result["metrics"]["mrr"] == 1.0
        assert all(task["channel_matches_expectation"] for task in result["tasks"])

        conflict = next(task for task in result["tasks"]
                        if task["id"] == "summary_name_conflict")
        assert conflict["actual_channel"] == "union"
        refs = [row["node_ref"] for row in conflict["selected_candidates"]]
        assert refs[0] == "src/summary.py::encrypt_session_tokens"
        assert "src/summary.py::persist_blob" in refs
        top = conflict["selected_candidates"][0]
        assert {"channel": "prefix", "rank": 1} in top["sources"]
        assert top["priority"] == 1

    def test_p1_4_navigation_measurement_dual_view(self, tmp_path):
        baseline = self._module()
        root = (Path(__file__).resolve().parents[1] / "eval" / "tasks" /
                "retrieval_baseline")
        p1_3 = baseline.load_spec(root / "p1_3.json")
        p1_4 = baseline.load_spec(root / "p1_4.json")
        assert p1_4["id"] == "p1_4_navigate_candidates"
        assert ({task["id"] for task in p1_4["tasks"]}
                == {task["id"] for task in p1_3["tasks"]})
        intents = p1_4["navigation"]["intents"]
        assert len(intents) == 3
        assert any(any("一" <= char <= "鿿" for char in intent)
                   for intent in intents)

        nav_db = tmp_path / "navigate.db"
        baseline.build_fixture(p1_4, nav_db).close()
        import index_mcp_queries as queries
        nav = baseline.navigation_measurement(queries, nav_db, intents, 5)
        assert nav["measured"] is True
        assert nav["corpus"] == {
            "cluster_count": 2, "file_count": 6, "file_with_short_count": 2,
        }
        records = {rec["intent"]: rec for rec in nav["intents"]}

        english = records["locate authentication token parser"]
        assert english["fallback_reason"] is None
        assert english["candidate_counts"] == {"cluster": 0, "file": 1, "symbol": 3}
        assert english["candidate_total"] == 4
        assert english["prompt_chars"] > 0
        assert english["corpus_chars"] > 0
        assert english["candidates_key"].startswith("navigate:")
        assert english["llm_called"] is False

        chinese = records["定位认证令牌解析器"]
        assert chinese["fallback_reason"] == "lexical_empty"
        assert chinese["candidate_total"] == 0
        assert chinese["prompt_chars"] == 0
        assert chinese["fallback_prompt_chars"] > 0

        mixed = records["认证 token 解析"]
        assert mixed["fallback_reason"] is None
        assert mixed["candidate_counts"]["symbol"] >= 1
        assert mixed["prompt_chars"] > 0

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

    @pytest.mark.parametrize("failing_channel", ["exact", "like", "fts", "fuzzy"])
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

        for channel in ("exact", "like", "fts", "fuzzy"):
            monkeypatch.setattr(index_mcp_queries, f"_search_{channel}", no_match)
        monkeypatch.setattr(index_mcp_queries, f"_search_{failing_channel}", fail)

        labels = {"exact": "EXACT", "like": "LIKE", "fts": "FTS", "fuzzy": "fuzzy"}
        result = index_mcp_queries.query_search_impl("missing")
        assert result == (
            f"Error: {labels[failing_channel]} search failed (OperationalError)."
        )
        assert "private database detail" not in result
        assert calls[-1] == failing_channel
        expected_calls = {"exact": 1, "like": 2, "fts": 3, "fuzzy": 4}[failing_channel]
        assert len(calls) == expected_calls

    def test_like_and_fuzzy_order_ignore_insertion_order(self):
        from index_mcp_queries import (
            _make_search_query, _search_exact, _search_fuzzy, _search_like,
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
            exact_query = _make_search_query("stableHandler", 10)
            exact = [(row[0], row[1]) for row in _search_exact(db, exact_query)]
            fuzzy_query = _make_search_query("stableHandlr", 10)
            fuzzy = [(row[0], row[1]) for row in _search_fuzzy(db, fuzzy_query)]
            db.close()
            return like, exact, fuzzy

        assert search(("b.py", "a.py")) == search(("a.py", "b.py"))


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
