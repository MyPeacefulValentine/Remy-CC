"""Tests for the current retrieval projection and FTS table."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL, VERSION
import retrieval_projection
import summarizer


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
    conn.execute(
        "INSERT INTO symbols (file_path, name, type, name_tokens, args) "
        "VALUES ('a.py', 'parse_input', 'function', 'parse input', 'raw')"
    )
    conn.execute(
        "INSERT INTO clusters (name, label, entry_symbols, file_count) "
        "VALUES ('parser', NULL, '[]', 1)"
    )
    retrieval_projection.rebuild_projection(conn)
    conn.commit()
    yield conn
    conn.close()


def _fts_refs(db, term):
    return db.execute(
        "SELECT d.node_kind, d.node_ref FROM retrieval_fts "
        "JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid "
        "WHERE retrieval_fts MATCH ?",
        (term,),
    ).fetchall()


class TestProjectionCreation:
    def test_projection_tables_exist(self, db):
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "retrieval_documents" in tables
        assert "retrieval_fts" in tables
        assert "summary_fts" not in tables

    def test_one_document_per_fact_node(self, db):
        rows = db.execute(
            "SELECT node_kind, node_ref FROM retrieval_documents "
            "ORDER BY node_kind, node_ref"
        ).fetchall()
        assert rows == [
            ("cluster", "parser"),
            ("file", "a.py"),
            ("symbol", "a.py::parse_input"),
        ]

    def test_symbol_without_summary_keeps_fact_fields(self, db):
        row = db.execute(
            "SELECT name, name_tokens, signature, summary_short "
            "FROM retrieval_documents WHERE node_kind='symbol'"
        ).fetchone()
        assert row == ("parse_input", "parse input", "raw", None)

    def test_projection_exposes_p1_structure_fields(self, db):
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(retrieval_documents)").fetchall()
        }
        assert {
            "language", "symbol_type", "file_path", "name", "name_tokens",
            "signature", "summary_short", "summary_full",
        }.issubset(columns)
        indexes = {
            row[1]
            for row in db.execute("PRAGMA index_list(retrieval_documents)").fetchall()
        }
        assert {"idx_retrieval_kind", "idx_retrieval_file"}.issubset(indexes)
        assert VERSION == "12.0.0"

    def test_shared_tokenizer_handles_supported_name_forms(self):
        from symbol_names import tokenize_symbol

        assert tokenize_symbol("parse_input_record") == "parse input record"
        assert tokenize_symbol("getUserById") == "get User By Id"
        assert tokenize_symbol("Auth::TokenParser") == "Auth Token Parser"


    def test_content_hash_is_stable_for_unchanged_content(self, db):
        first = retrieval_projection.refresh_node(
            db, "symbol", "a.py::parse_input"
        )["content_hash"]
        second = retrieval_projection.refresh_node(
            db, "symbol", "a.py::parse_input"
        )["content_hash"]
        assert first == second

    def test_content_hash_changes_with_retrieval_content(self, db):
        before = retrieval_projection.refresh_node(
            db, "symbol", "a.py::parse_input"
        )["content_hash"]
        db.execute(
            "UPDATE symbols SET args='raw, strict' "
            "WHERE file_path='a.py' AND name='parse_input'"
        )
        after = retrieval_projection.refresh_node(
            db, "symbol", "a.py::parse_input"
        )["content_hash"]
        assert before != after
    def test_content_hash_ignores_summary_version(self, db):
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "same content", "full": None}, "ok"
        )
        first = db.execute(
            "SELECT content_hash, source_version FROM retrieval_documents "
            "WHERE node_kind='symbol' AND node_ref='a.py::parse_input'"
        ).fetchone()
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "same content", "full": None}, "ok"
        )
        second = db.execute(
            "SELECT content_hash, source_version FROM retrieval_documents "
            "WHERE node_kind='symbol' AND node_ref='a.py::parse_input'"
        ).fetchone()
        assert first[0] == second[0]
        assert first[1:] == (1,)
        assert second[1:] == (2,)


class TestCurrentSummarySync:
    def test_summary_write_refreshes_single_document(self, db):
        summarizer.write_summary_version(
            db,
            "symbol",
            "a.py::parse_input",
            {"short": "parses raw input lines", "full": None},
            "ok",
        )
        assert _fts_refs(db, "parses") == [("symbol", "a.py::parse_input")]
        count = db.execute(
            "SELECT COUNT(*) FROM retrieval_documents "
            "WHERE node_kind='symbol' AND node_ref='a.py::parse_input'"
        ).fetchone()[0]
        assert count == 1

    def test_new_version_replaces_old_fts_text(self, db):
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "legacykeyword parser", "full": None}, "ok"
        )
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "currentkeyword parser", "full": None}, "ok"
        )
        assert _fts_refs(db, "legacykeyword") == []
        assert _fts_refs(db, "currentkeyword") == [
            ("symbol", "a.py::parse_input")
        ]

    @pytest.mark.parametrize("status", ["pending", "corrupt", "oversized_hard"])
    def test_temporary_failure_falls_back(self, db, status):
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "usablekeyword", "full": None}, "ok"
        )
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input", None, status
        )
        assert _fts_refs(db, "usablekeyword") == [
            ("symbol", "a.py::parse_input")
        ]

    def test_oversized_warn_is_usable(self, db):
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "warnkeyword", "full": None}, "oversized_warn"
        )
        assert _fts_refs(db, "warnkeyword") == [
            ("symbol", "a.py::parse_input")
        ]

    def test_stale_blocks_older_summary(self, db):
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "blockedkeyword", "full": None}, "ok"
        )
        assert retrieval_projection.mark_current_summary_stale(
            db, "symbol", "a.py::parse_input"
        )
        assert _fts_refs(db, "blockedkeyword") == []

    def test_vacuum_protection_keeps_latest_event_and_selected_summary(self, db):
        first = summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "selected", "full": None}, "ok"
        )
        second = summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input", None, "pending"
        )
        protected = retrieval_projection.protected_summary_ids(db)
        ids = dict(db.execute(
            "SELECT version, id FROM summary_versions "
            "WHERE node_kind='symbol' AND node_ref='a.py::parse_input'"
        ).fetchall())
        assert first == 1
        assert second == 2
        assert protected.issuperset({ids[1], ids[2]})

    def test_delete_fact_removes_projection_and_fts(self, db):
        summarizer.write_summary_version(
            db, "symbol", "a.py::parse_input",
            {"short": "deletekeyword", "full": None}, "ok"
        )
        retrieval_projection.delete_node(db, "symbol", "a.py::parse_input")
        db.execute(
            "DELETE FROM symbols WHERE file_path='a.py' AND name='parse_input'"
        )
        db.commit()
        assert _fts_refs(db, "deletekeyword") == []
        assert db.execute(
            "SELECT COUNT(*) FROM retrieval_documents "
            "WHERE node_ref='a.py::parse_input'"
        ).fetchone()[0] == 0


class TestFactLookupPredicateEquivalence:
    """The split (file_path, name) lookup plus the expression fallback must
    resolve exactly the rows the original concat predicate resolved,
    including "::" collisions inside names and stored paths."""

    _EXPRESSION_QUERY = (
        "SELECT s.file_path, s.name FROM symbols s "
        "JOIN files f ON f.path = s.file_path "
        "WHERE s.file_path || '::' || s.name = ?"
    )

    def _seed(self, db, pairs):
        for file_path, name in pairs:
            db.execute(
                "INSERT OR IGNORE INTO files (path, struct_hash) VALUES (?, 'h')",
                (file_path,),
            )
            db.execute(
                "INSERT OR IGNORE INTO symbols (file_path, name, type, name_tokens) "
                "VALUES (?, ?, 'function', '')",
                (file_path, name),
            )

    def test_randomized_refs_match_expression_predicate(self, db):
        import random

        rng = random.Random(20260820)
        segments = ["a", "b", "src/x", "n::s", "p::q/r", "m.py", "impl::T"]
        pairs = set()
        while len(pairs) < 60:
            file_path = rng.choice(segments) + rng.choice(["", ".py", ".rs"])
            name = rng.choice(segments).replace("/", ".")
            pairs.add((file_path, name))
        self._seed(db, pairs)
        db.commit()

        refs = {f"{fp}::{name}" for fp, name in pairs}
        refs.add("missing.py::nope")
        refs.add("a")
        for node_ref in sorted(refs):
            document = retrieval_projection._load_fact_document(
                db, "symbol", node_ref
            )
            # An ambiguous ref (several rows concatenating to the same
            # string) never had a specified row order under fetchone, so
            # equivalence is membership in the expression result set.
            expected = {
                tuple(row)
                for row in db.execute(self._EXPRESSION_QUERY, (node_ref,)).fetchall()
            }
            if not expected:
                assert document is None, node_ref
            else:
                assert document is not None, node_ref
                assert (document["file_path"], document["name"]) in expected, node_ref

    def test_pathological_path_containing_separator_uses_fallback(self, db):
        self._seed(db, [("weird::dir/f.py", "run")])
        db.commit()
        document = retrieval_projection._load_fact_document(
            db, "symbol", "weird::dir/f.py::run"
        )
        assert document is not None
        assert document["file_path"] == "weird::dir/f.py"
        assert document["name"] == "run"
