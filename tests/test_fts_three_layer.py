"""Tests for the three-layer summary_fts virtual table."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL, VERSION
import summarizer


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
    conn.execute(
        "INSERT INTO symbols (file_path, name, type, name_tokens) "
        "VALUES ('a.py', 'parse_input', 'function', 'parse input')"
    )
    conn.execute(
        "INSERT INTO clusters (name, label, entry_symbols, file_count) "
        "VALUES ('parser', NULL, '[]', 1)"
    )
    conn.commit()
    yield conn
    conn.close()


def _fts_results(db, term):
    return db.execute(
        "SELECT node_kind, node_ref, short FROM summary_fts WHERE summary_fts MATCH ?",
        (term,),
    ).fetchall()


class TestFTSCreation:
    def test_summary_fts_exists(self, db):
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE name='summary_fts'"
        ).fetchone()
        assert row is not None

    def test_symbols_fts_removed_in_v7_schema(self, db):
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE name='symbols_fts'"
        ).fetchone()
        assert row is None


class TestThreeLayerSync:
    def test_symbol_insert_indexed(self, db):
        summarizer.write_summary_version(db, "symbol", "a.py::parse_input",
                                         {"short": "[Doc] parses raw input lines", "full": None}, "ok")
        results = _fts_results(db, "parses")
        assert len(results) == 1
        assert results[0][0] == "symbol"
        assert results[0][1] == "a.py::parse_input"

    def test_file_insert_indexed(self, db):
        summarizer.write_summary_version(db, "file", "a.py",
                                         {"short": "Parses CLI arguments", "full": None}, "ok")
        results = _fts_results(db, "CLI")
        assert len(results) == 1
        assert results[0][0] == "file"

    def test_cluster_insert_indexed(self, db):
        summarizer.write_summary_version(db, "cluster", "parser",
                                         {"short": "Parsing subsystem", "full": "[定位] tokens"}, "ok")
        results = _fts_results(db, "subsystem")
        assert len(results) == 1
        assert results[0][0] == "cluster"

    def test_pending_status_not_indexed(self, db):
        summarizer.write_summary_version(db, "file", "a.py",
                                         None, "pending")
        results = _fts_results(db, "anything")
        assert results == []

    def test_corrupt_status_not_indexed(self, db):
        summarizer.write_summary_version(db, "file", "a.py",
                                         {"short": "garbage", "full": None}, "corrupt")
        results = _fts_results(db, "garbage")
        assert results == []

    def test_delete_removes_from_fts(self, db):
        summarizer.write_summary_version(db, "file", "a.py",
                                         {"short": "to be deleted", "full": None}, "ok")
        assert len(_fts_results(db, "deleted")) == 1
        db.execute("DELETE FROM summary_versions WHERE node_kind='file' AND node_ref='a.py'")
        db.commit()
        assert _fts_results(db, "deleted") == []

    def test_new_version_indexed_alongside_old(self, db):
        summarizer.write_summary_version(db, "file", "a.py",
                                         {"short": "first version", "full": None}, "ok")
        summarizer.write_summary_version(db, "file", "a.py",
                                         {"short": "second version", "full": None}, "ok")
        results = _fts_results(db, "version")
        assert len(results) == 2


class TestNodeKindFilter:
    def test_filter_by_symbol(self, db):
        summarizer.write_summary_version(db, "symbol", "a.py::parse_input",
                                         {"short": "common word here", "full": None}, "ok")
        summarizer.write_summary_version(db, "file", "a.py",
                                         {"short": "common word also", "full": None}, "ok")
        rows = db.execute(
            "SELECT node_ref FROM summary_fts WHERE summary_fts MATCH 'common' "
            "AND node_kind = 'symbol'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "a.py::parse_input"
