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
