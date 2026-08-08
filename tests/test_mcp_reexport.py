"""Tests for the index_mcp_queries compatibility re-export shell (A1.2/A1.3).

The shell's full import surface is pinned bidirectionally: removing a
re-export, adding an unpinned one, or moving a name to a different owner
module all fail ``test_shell_surface_equals_pinned_bindings``.
"""
import ast
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

_SHELL_PATH = os.path.join(os.path.dirname(__file__), "..", "remy-src", "index_mcp_queries.py")

_IMPL_NAMES = (
    "query_symbol_impl", "query_symbol_summary_impl", "query_file_summary_impl",
    "query_callers_impl", "query_callees_impl", "query_impact_impl",
    "query_patterns_impl", "query_search_impl", "query_flow_impl",
    "query_cluster_summary_impl", "query_cluster_files_impl", "query_navigate_impl",
)

_REEXPORT_SURFACE = {
    "index_mcp_common": (
        "DB_FILE_DEFAULT", "_DB_NOT_FOUND", "_DB_OVERRIDE", "_IMPACT_DIR",
        "_QUERY_CONFIG", "_config", "_config_values", "_open_db",
        "_query_scoped", "database_override", "get_latest_summary",
    ),
    "impact": (
        "_bfs_callers", "_bfs_callees", "collect_file_symbols",
        "get_layer", "get_line_range",
    ),
    "struct_scan": ("tokenize_symbol",),
    "retrieval_projection": ("select_current_summary",),
    "index_mcp_facts": (
        "_resolve_symbol", "query_cluster_files_impl", "query_cluster_summary_impl",
        "query_file_summary_impl", "query_patterns_impl", "query_symbol_impl",
        "query_symbol_summary_impl",
    ),
    "index_mcp_graph": (
        "_IMPACT_LABELS_PER_LEVEL", "_bfs_callers_ambiguous", "_bfs_callees_ambiguous",
        "_bidir_bfs", "_format_bfs_result", "_format_flow", "_format_impact_result",
        "_impact_level_files", "_load_graph", "_reconstruct_path",
        "_resolve_flow_symbol", "query_callees_impl", "query_callers_impl",
        "query_flow_impl", "query_impact_impl",
    ),
    "index_mcp_search": (
        "_CHANNEL_PRIORITY", "_FUZZY_CUTOFF", "_LANGUAGE_VALUES", "_MATCH_MODES",
        "_SYMBOL_TYPES", "_SearchInputError", "_SearchQuery", "_append_search_filters",
        "_casefold_text", "_channel_error", "_coerce_search_query", "_contains_phrase",
        "_extract_search_words", "_fts_available", "_fts_expression", "_fts_rows",
        "_like_sort_key", "_make_search_query", "_merge_candidates", "_normalize_path",
        "_register_search_functions", "_result_detail", "_search_exact", "_search_fts",
        "_search_fuzzy", "_search_like", "_word_prefix_count", "query_search_impl",
    ),
    "index_mcp_navigate": (
        "_NAVIGATE_DOC_COLUMNS", "_NAVIGATE_DOC_WEIGHTS", "_NAVIGATE_KIND_ORDER",
        "_NAVIGATE_PROMPT_VERSION", "_build_navigate_prompt", "_cluster_fallback_candidates",
        "_collect_cluster_corpus", "_collect_navigate_corpus", "_file_cluster_map",
        "_format_navigate", "_heuristic_navigate", "_navigate_cache_key",
        "_navigate_candidates", "_navigate_doc_rows", "_navigate_quotas",
        "_navigate_symbol_docs", "_navigate_symbol_rows", "_normalize_intent",
        "_parse_navigate_response", "_try_default_llm_call", "query_navigate_impl",
    ),
}

_PLAIN_IMPORTS = ("remy_config",)

_ALIASED = {"_bfs_callers": "bfs_callers", "_bfs_callees": "bfs_callees"}


def _shell_import_bindings():
    """Map each name bound by the shell to ``(owner_module, owner_attr)``.

    ``owner_attr`` is None for plain ``import module`` statements.
    """
    with open(_SHELL_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=_SHELL_PATH)
    bindings = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name] = (alias.name, None)
    return bindings


def _expected_bindings():
    expected = {}
    for module_name, names in _REEXPORT_SURFACE.items():
        for name in names:
            expected[name] = (module_name, _ALIASED.get(name, name))
    for name in _PLAIN_IMPORTS:
        expected[name] = (name, None)
    return expected


class TestReExportSurface:
    def test_all_impls_are_importable(self):
        import index_mcp_queries
        for name in _IMPL_NAMES:
            assert callable(getattr(index_mcp_queries, name)), name

    def test_impl_names_are_part_of_pinned_surface(self):
        pinned = {name for names in _REEXPORT_SURFACE.values() for name in names}
        assert set(_IMPL_NAMES) <= pinned

    def test_shell_surface_equals_pinned_bindings(self):
        assert _shell_import_bindings() == _expected_bindings()

    def test_every_pinned_name_is_owner_object(self):
        import index_mcp_queries
        for bound_name, (module_name, owner_attr) in _expected_bindings().items():
            owner = importlib.import_module(module_name)
            if owner_attr is None:
                assert getattr(index_mcp_queries, bound_name) is owner, bound_name
            else:
                assert getattr(index_mcp_queries, bound_name) is getattr(owner, owner_attr), bound_name

    def test_database_override_reaches_all_regions(self, db_dir, tmp_path):
        import index_mcp_queries as q
        db_path = tmp_path / ".claude" / "logic_index.db"
        with q.database_override(db_path):
            assert "a.py::main" in q.query_symbol_impl("main", None)
            assert "a.py::main" in q.query_callers_impl("b.py::process", 2, False, False)
            assert "b.py::process" in q.query_search_impl("process", limit=5)
