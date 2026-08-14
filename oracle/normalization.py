"""Single owner of table-level normalized projections over logic_index.db.

Builds deterministic, autoincrement-free projections of the scanner fact
tables. Two views are published:

- oracle_state: every classified column (classification.VIEWS order),
  consumed by the R3 comparator.
- canary_state: the historical TEE-canary view — identical to oracle_state
  minus the trailing columns in CANARY_EXCLUDED_COLUMNS. Kept so the
  long-standing canary equality baselines stay byte-compatible.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from . import classification

_FROM = {
    "files": "files",
    "symbols": "symbols",
    "symbol_occurrences": "symbol_occurrences",
    "edges": "edges",
    "edge_candidates": "edge_candidates ec JOIN edges e ON e.id=ec.edge_id",
    "patterns": "patterns",
    "clusters": "clusters",
    "cluster_members": "cluster_members cm JOIN clusters c ON c.id=cm.cluster_id",
    "retrieval_documents": "retrieval_documents",
}

# Column expressions for joined views; plain views use the bare column name.
_COLUMN_SQL = {
    ("edge_candidates", "source_file"): "e.source_file",
    ("edge_candidates", "caller"): "e.caller",
    ("edge_candidates", "callee"): "e.callee",
    ("edge_candidates", "line"): "e.line",
    ("edge_candidates", "candidate_qualified"): "ec.candidate_qualified",
    ("edge_candidates", "score"): "ec.score",
    ("cluster_members", "cluster"): "c.name",
    ("cluster_members", "file_path"): "cm.file_path",
}

_ORDER_BY = {
    "files": ("path",),
    "symbols": ("file_path", "name"),
    "symbol_occurrences": ("file_path", "name", "occurrence_index"),
    "edges": ("source_file", "caller", "callee", "callee_qualified", "line", "provenance", "via"),
    "edge_candidates": ("source_file", "caller", "callee", "line", "candidate_qualified"),
    "patterns": ("file_path", "pattern_type", "signal_name", "handler", "line", "metadata"),
    "clusters": ("name",),
    "cluster_members": ("cluster", "file_path"),
    "retrieval_documents": ("node_kind", "node_ref"),
}

# Columns absent from the historical canary view. Suffix-only by contract,
# so filtering preserves the original column order.
CANARY_EXCLUDED_COLUMNS = {
    "files": ("import_bindings",),
    "edges": ("call_form",),
}


def _expr(view: str, column: str) -> str:
    return _COLUMN_SQL.get((view, column), column)


def view_sql(view: str, columns: Sequence[str]) -> str:
    select = ",".join(_expr(view, column) for column in columns)
    order = ",".join(_expr(view, column) for column in _ORDER_BY[view])
    return f"SELECT {select} FROM {_FROM[view]} ORDER BY {order}"


def oracle_columns() -> dict[str, tuple[str, ...]]:
    return {
        view: tuple(column for column, _cls in spec["columns"])
        for view, spec in classification.VIEWS.items()
    }


def canary_columns() -> dict[str, tuple[str, ...]]:
    columns = oracle_columns()
    return {
        view: tuple(
            column
            for column in names
            if column not in CANARY_EXCLUDED_COLUMNS.get(view, ())
        )
        for view, names in columns.items()
    }


def state(
    db: sqlite3.Connection, view_columns: Mapping[str, Sequence[str]]
) -> dict[str, list[tuple[Any, ...]]]:
    return {
        view: db.execute(view_sql(view, columns)).fetchall()
        for view, columns in view_columns.items()
    }


def oracle_state(db: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return state(db, oracle_columns())


def canary_state(db: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return state(db, canary_columns())
