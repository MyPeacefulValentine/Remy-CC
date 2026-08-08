#!/usr/bin/env python3
"""Compatibility re-export entry for the split remy-index MCP query modules.

Implementations moved in A1.2 to index_mcp_common / index_mcp_facts /
index_mcp_graph / index_mcp_search / index_mcp_navigate. This module keeps
every previously public name importable for at least one release cycle.
"""
from index_mcp_common import (
    DB_FILE_DEFAULT,
    _DB_NOT_FOUND,
    _DB_OVERRIDE,
    _IMPACT_DIR,
    _QUERY_CONFIG,
    _config,
    _config_values,
    _open_db,
    _query_scoped,
    database_override,
    get_latest_summary,
)
from impact import (
    bfs_callers as _bfs_callers,
    bfs_callees as _bfs_callees,
    collect_file_symbols,
    get_layer,
    get_line_range,
)
from struct_scan import tokenize_symbol
from retrieval_projection import select_current_summary
import remy_config
from index_mcp_facts import (
    _resolve_symbol,
    query_cluster_files_impl,
    query_cluster_summary_impl,
    query_file_summary_impl,
    query_patterns_impl,
    query_symbol_impl,
    query_symbol_summary_impl,
)
from index_mcp_graph import (
    _IMPACT_LABELS_PER_LEVEL,
    _bfs_callers_ambiguous,
    _bfs_callees_ambiguous,
    _bidir_bfs,
    _format_bfs_result,
    _format_flow,
    _format_impact_result,
    _impact_level_files,
    _load_graph,
    _reconstruct_path,
    _resolve_flow_symbol,
    query_callees_impl,
    query_callers_impl,
    query_flow_impl,
    query_impact_impl,
)
from index_mcp_search import (
    _CHANNEL_PRIORITY,
    _FUZZY_CUTOFF,
    _LANGUAGE_VALUES,
    _MATCH_MODES,
    _SYMBOL_TYPES,
    _SearchInputError,
    _SearchQuery,
    _append_search_filters,
    _casefold_text,
    _channel_error,
    _coerce_search_query,
    _contains_phrase,
    _extract_search_words,
    _fts_available,
    _fts_expression,
    _fts_rows,
    _like_sort_key,
    _make_search_query,
    _merge_candidates,
    _normalize_path,
    _register_search_functions,
    _result_detail,
    _search_exact,
    _search_fts,
    _search_fuzzy,
    _search_like,
    _word_prefix_count,
    query_search_impl,
)
from index_mcp_navigate import (
    _NAVIGATE_DOC_COLUMNS,
    _NAVIGATE_DOC_WEIGHTS,
    _NAVIGATE_KIND_ORDER,
    _NAVIGATE_PROMPT_VERSION,
    _build_navigate_prompt,
    _cluster_fallback_candidates,
    _collect_cluster_corpus,
    _collect_navigate_corpus,
    _file_cluster_map,
    _format_navigate,
    _heuristic_navigate,
    _navigate_cache_key,
    _navigate_candidates,
    _navigate_doc_rows,
    _navigate_quotas,
    _navigate_symbol_docs,
    _navigate_symbol_rows,
    _normalize_intent,
    _parse_navigate_response,
    _try_default_llm_call,
    query_navigate_impl,
)
