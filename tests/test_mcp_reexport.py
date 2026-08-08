"""Tests for the index_mcp_queries compatibility re-export shell (A1.2)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

_IMPL_NAMES = (
    "query_symbol_impl", "query_symbol_summary_impl", "query_file_summary_impl",
    "query_callers_impl", "query_callees_impl", "query_impact_impl",
    "query_patterns_impl", "query_search_impl", "query_flow_impl",
    "query_cluster_summary_impl", "query_cluster_files_impl", "query_navigate_impl",
)

_COMPAT_NAMES = (
    "DB_FILE_DEFAULT", "database_override", "get_latest_summary",
    "_open_db", "_config", "_config_values", "_query_scoped",
    "_DB_NOT_FOUND", "_DB_OVERRIDE", "_QUERY_CONFIG", "_IMPACT_DIR",
    "_resolve_symbol",
    "_bfs_callers", "_bfs_callees", "_bfs_callers_ambiguous",
    "_bfs_callees_ambiguous", "_load_graph", "_bidir_bfs",
    "_resolve_flow_symbol", "_format_flow", "_format_bfs_result",
    "_format_impact_result", "_impact_level_files", "_IMPACT_LABELS_PER_LEVEL",
    "_SearchQuery", "_SearchInputError", "_make_search_query",
    "_search_exact", "_search_like", "_search_fts", "_search_fuzzy",
    "_merge_candidates", "_result_detail", "_extract_search_words",
    "_register_search_functions", "_fts_expression", "_fts_available",
    "_navigate_candidates", "_navigate_cache_key", "_navigate_quotas",
    "_collect_cluster_corpus", "_collect_navigate_corpus",
    "_cluster_fallback_candidates", "_build_navigate_prompt",
    "_parse_navigate_response", "_heuristic_navigate", "_format_navigate",
    "_try_default_llm_call",
    "collect_file_symbols", "get_layer", "get_line_range",
    "tokenize_symbol", "select_current_summary", "remy_config",
)


class TestReExportSurface:
    def test_all_impls_are_importable(self):
        import index_mcp_queries
        for name in _IMPL_NAMES:
            assert callable(getattr(index_mcp_queries, name)), name

    def test_compat_names_are_importable(self):
        import index_mcp_queries
        missing = [name for name in _COMPAT_NAMES
                   if not hasattr(index_mcp_queries, name)]
        assert missing == []

    def test_reexports_are_owner_objects(self):
        import index_mcp_queries
        import index_mcp_common
        import index_mcp_facts
        import index_mcp_graph
        import index_mcp_search
        import index_mcp_navigate
        pairs = (
            (index_mcp_common, "database_override"),
            (index_mcp_common, "_open_db"),
            (index_mcp_facts, "query_symbol_impl"),
            (index_mcp_graph, "query_flow_impl"),
            (index_mcp_search, "query_search_impl"),
            (index_mcp_navigate, "query_navigate_impl"),
        )
        for owner, name in pairs:
            assert getattr(index_mcp_queries, name) is getattr(owner, name), name

    def test_database_override_reaches_all_regions(self, db_dir, tmp_path):
        import index_mcp_queries as q
        db_path = tmp_path / ".claude" / "logic_index.db"
        with q.database_override(db_path):
            assert "a.py::main" in q.query_symbol_impl("main", None)
            assert "a.py::main" in q.query_callers_impl("b.py::process", 2, False, False)
            assert "b.py::process" in q.query_search_impl("process", limit=5)
