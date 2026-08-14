"""Declarative field classification for the oracle comparator.

Maps every fact table of logic index schema 12.0.0 to a comparison view:
which columns identify a row, and whether each column must match exactly
between two scanner implementations ("exact") or may legitimately differ
("allowed_diff", e.g. LLM-generated text). Tables that hold runtime or
audit records rather than scanner facts are diagnostic-only and excluded
from comparison.

Any change to this module is a change of the oracle identity and MUST
bump CLASSIFICATION_VERSION.
"""

CLASSIFICATION_VERSION = "1"

EXACT = "exact"
ALLOWED_DIFF = "allowed_diff"

DIAGNOSTIC_TABLES = (
    "summary_versions",
    "node_change_counters",
    "judge_cache",
    "migration_log",
    "meta",
)

# view name -> {"key": key column tuple or None (multiset semantics),
#               "columns": ((column, class), ...)}
# Column order is part of the contract: the historical canary view is the
# same order with the trailing columns listed in normalization's
# CANARY_EXCLUDED_COLUMNS removed.
VIEWS = {
    "files": {
        "key": ("path",),
        "columns": (
            ("path", EXACT),
            ("struct_hash", EXACT),
            ("language", EXACT),
            ("layer", EXACT),
            ("imports", EXACT),
            ("kind_hint", EXACT),
            ("actual_kind", EXACT),
            ("parser_contract_version", EXACT),
            ("parser_backend", EXACT),
            ("parser_environment", EXACT),
            ("import_bindings", EXACT),
        ),
    },
    "symbols": {
        "key": ("file_path", "name"),
        "columns": (
            ("file_path", EXACT),
            ("name", EXACT),
            ("short_name", EXACT),
            ("type", EXACT),
            ("args", EXACT),
            ("lineno", EXACT),
            ("end_lineno", EXACT),
            ("hash", EXACT),
            ("bases", EXACT),
            ("name_tokens", EXACT),
        ),
    },
    "symbol_occurrences": {
        "key": ("file_path", "name", "occurrence_index"),
        "columns": (
            ("file_path", EXACT),
            ("name", EXACT),
            ("occurrence_index", EXACT),
            ("type", EXACT),
            ("args", EXACT),
            ("lineno", EXACT),
            ("end_lineno", EXACT),
            ("hash", EXACT),
            ("is_canonical", EXACT),
            ("conflict_kind", EXACT),
            ("selection_reason", EXACT),
        ),
    },
    "edges": {
        "key": None,
        "columns": (
            ("source_file", EXACT),
            ("caller", EXACT),
            ("callee", EXACT),
            ("callee_file", EXACT),
            ("callee_qualified", EXACT),
            ("line", EXACT),
            ("provenance", EXACT),
            ("synthesized_from", EXACT),
            ("via", EXACT),
            ("call_form", EXACT),
        ),
    },
    "edge_candidates": {
        "key": None,
        "columns": (
            ("source_file", EXACT),
            ("caller", EXACT),
            ("callee", EXACT),
            ("line", EXACT),
            ("candidate_qualified", EXACT),
            ("score", EXACT),
        ),
    },
    "patterns": {
        "key": None,
        "columns": (
            ("file_path", EXACT),
            ("pattern_type", EXACT),
            ("signal_name", EXACT),
            ("handler", EXACT),
            ("line", EXACT),
            ("metadata", EXACT),
        ),
    },
    "clusters": {
        "key": ("name",),
        "columns": (
            ("name", EXACT),
            ("label", EXACT),
            ("entry_symbols", EXACT),
            ("file_count", EXACT),
        ),
    },
    "cluster_members": {
        "key": None,
        "columns": (
            ("cluster", EXACT),
            ("file_path", EXACT),
        ),
    },
    "retrieval_documents": {
        "key": ("node_kind", "node_ref"),
        "columns": (
            ("node_kind", EXACT),
            ("node_ref", EXACT),
            ("language", EXACT),
            ("symbol_type", EXACT),
            ("file_path", EXACT),
            ("name", EXACT),
            ("name_tokens", EXACT),
            ("signature", EXACT),
            ("summary_short", ALLOWED_DIFF),
            ("summary_full", ALLOWED_DIFF),
            ("content_hash", ALLOWED_DIFF),
        ),
    },
}


def column_classes(view: str) -> dict:
    """Return {column: class} for one view."""
    return dict(VIEWS[view]["columns"])
