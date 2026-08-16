"""Contract tests: schema.sql is the single DDL source shared with Rust."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import schema
from retrieval_projection import RETRIEVAL_SCHEMA_SQL

SCHEMA_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "remy-index" / "schema.sql"
)


def test_schema_sql_matches_file_content():
    assert schema.SCHEMA_SQL == SCHEMA_SQL_PATH.read_text(encoding="utf-8")


def test_schema_sql_embeds_retrieval_projection_ddl():
    assert schema.SCHEMA_SQL.endswith(RETRIEVAL_SCHEMA_SQL)


def test_schema_sql_creates_all_contract_tables(tmp_path: Path):
    db = sqlite3.connect(str(tmp_path / "schema.db"))
    try:
        db.executescript(schema.SCHEMA_SQL)
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        db.close()
    assert {
        "files", "symbols", "symbol_occurrences", "edges", "edge_candidates",
        "patterns", "clusters", "cluster_members", "summary_versions",
        "node_change_counters", "judge_cache", "migration_log", "meta",
        "retrieval_documents",
    } <= tables
