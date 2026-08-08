"""Tests for index_mcp_search.py — exact/prefix/BM25/fuzzy retrieval."""
import json
import os
import sqlite3
import sys

import pytest

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
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


class TestQuerySearch:
    def test_union_prefix_match(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("proc", limit=5)
        assert "process" in result
        assert "matched via union" in result

    def test_fts_exact_name_ranked_first(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("main", limit=5)
        lines = [l for l in result.splitlines() if "a.py::main" in l]
        assert len(lines) == 1

    def test_union_reports_sources_priority_and_detail(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("main", limit=5)
        assert "sources: exact#1, prefix#1 | priority=0" in result
        assert "sig: (args) | summary: entry point" in result

    def test_prefix_only_query_matches_via_union(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("hel", limit=5)
        assert "helper" in result
        assert "matched via union" in result
        assert "sources: prefix#1 | priority=1" in result

    def test_fuzzy_fallback_on_typo(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("processs", limit=5)
        assert "process" in result
        assert "matched via fuzzy" in result
        assert "sources: fuzzy#1 | priority=3" in result

    def test_file_hint_filters(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("main", limit=5, file_hint="b.py")
        assert "a.py" not in result

    def test_no_match_returns_message(self, db_dir):
        from index_mcp_search import query_search_impl
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
        from index_mcp_search import query_search_impl
        result = query_search_impl("test", limit=5)
        assert "FTS index not available" in result

    def test_limit_respected(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("main", limit=1)
        content_lines = [l for l in result.splitlines() if l.strip().startswith("[")]
        assert len(content_lines) <= 1

    def test_exact_name_ranks_above_prefix(self, db_dir):
        from index_mcp_search import query_search_impl
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
        from index_mcp_search import query_search_impl
        result = query_search_impl("get User", limit=5)
        assert "getUserById" in result
        assert "sig:" not in result
        assert "summary:" not in result

    def test_empty_string_input(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("", limit=5)
        assert isinstance(result, str)

    def test_special_characters_input(self, db_dir):
        from index_mcp_search import query_search_impl
        result = query_search_impl("proc*", limit=5)
        assert isinstance(result, str)
        assert "error" not in result.lower() or "No symbols" in result

    def test_exact_channel_respects_language_and_type_filters(self, db_dir):
        from index_mcp_search import query_search_impl
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
        from index_mcp_search import _merge_candidates

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
        import index_mcp_search

        def forbidden(_db, _query):
            raise AssertionError("fuzzy must not run with deterministic candidates")

        monkeypatch.setattr(index_mcp_search, "_search_fuzzy", forbidden)
        result = index_mcp_search.query_search_impl("helper", limit=5)
        assert "matched via union" in result

    def test_exact_ignores_match_mode(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "AlphaBeta", "Alpha Beta", None),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_search import query_search_impl
        result = query_search_impl("alphabeta", limit=5, match="phrase")
        assert "m.py::AlphaBeta" in result
        assert "sources: exact#1 | priority=0" in result

    def test_exact_uses_unicode_casefold(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "Straße", "Straße", None),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_search import query_search_impl
        result = query_search_impl("STRASSE", limit=5)
        assert "m.py::Straße" in result
        assert "exact#1" in result

    def test_exact_name_survives_summary_hits(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "process", "process", "other work"),
            ("m.py", "alpha", "alpha", "process everything nightly"),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_search import query_search_impl
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
        from index_mcp_search import query_search_impl
        result = query_search_impl("proc", limit=5)
        assert result.index("p.py::proc_one") < result.index("p.py::beta")

    def test_truncation_applies_after_merge(self, tmp_path, monkeypatch):
        _write_union_db(tmp_path, [
            ("m.py", "process", "process", "other work"),
            ("m.py", "alpha", "alpha", "process everything nightly"),
        ])
        monkeypatch.chdir(tmp_path)
        from index_mcp_search import query_search_impl
        result = query_search_impl("process", limit=1)
        assert "m.py::process" in result
        assert "m.py::alpha" not in result

    @pytest.mark.parametrize("text", ["", "   ", "*** (:)"])
    def test_invalid_query_returns_error(self, db_dir, text):
        from index_mcp_search import query_search_impl

        assert query_search_impl(text).startswith("Error:")

    @pytest.mark.parametrize("field,value", [
        ("match", "invalid"),
        ("language", "rust"),
        ("language", "   "),
        ("symbol_type", "method"),
        ("symbol_type", "   "),
    ])
    def test_invalid_enum_returns_error(self, db_dir, field, value):
        from index_mcp_search import query_search_impl

        assert query_search_impl("target", **{field: value}).startswith("Error:")

    def test_path_alias_conflict_returns_error(self, db_dir):
        from index_mcp_search import query_search_impl

        result = query_search_impl(
            "target", file_hint="src/", path_hint="tests/"
        )
        assert result.startswith("Error:")

    def test_path_nul_returns_error(self, db_dir):
        from index_mcp_search import query_search_impl

        result = query_search_impl("target", path_hint="src/\0file")
        assert result.startswith("Error:")
        assert "NUL" in result

    def test_path_alias_equivalence_and_normalization(self, db_dir):
        from index_mcp_search import query_search_impl

        result = query_search_impl(
            "main", file_hint="A.PY", path_hint="a.py"
        )
        assert "a.py::main" in result

    def test_language_and_type_filters(self, db_dir):
        from index_mcp_search import query_search_impl

        result = query_search_impl(
            "entry", language="python", symbol_type="function"
        )
        assert "a.py::main" in result
        assert query_search_impl("entry", language="c_cpp").startswith(
            "No symbols found"
        )

    def test_match_modes(self, db_dir):
        from index_mcp_search import query_search_impl

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

        from index_mcp_search import query_search_impl
        result = query_search_impl("duplicateHandlr", limit=1)
        lines = [line for line in result.splitlines() if line.strip().startswith("[")]
        assert len(lines) == 1
        assert "a.py::duplicateHandler" in lines[0]

    @pytest.mark.parametrize("failing_channel", ["exact", "like", "fts", "fuzzy"])
    def test_channel_sqlite_error_stops_fallback(self, db_dir, monkeypatch,
                                                 failing_channel):
        import index_mcp_search

        calls = []

        def no_match(_db, _query):
            calls.append("no_match")
            return []

        def fail(_db, _query):
            calls.append(failing_channel)
            raise sqlite3.OperationalError("private database detail")

        for channel in ("exact", "like", "fts", "fuzzy"):
            monkeypatch.setattr(index_mcp_search, f"_search_{channel}", no_match)
        monkeypatch.setattr(index_mcp_search, f"_search_{failing_channel}", fail)

        labels = {"exact": "EXACT", "like": "LIKE", "fts": "FTS", "fuzzy": "fuzzy"}
        result = index_mcp_search.query_search_impl("missing")
        assert result == (
            f"Error: {labels[failing_channel]} search failed (OperationalError)."
        )
        assert "private database detail" not in result
        assert calls[-1] == failing_channel
        expected_calls = {"exact": 1, "like": 2, "fts": 3, "fuzzy": 4}[failing_channel]
        assert len(calls) == expected_calls

    def test_like_and_fuzzy_order_ignore_insertion_order(self):
        from index_mcp_search import (
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
        from index_mcp_common import _open_db
        from index_mcp_search import _search_fts
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
        from index_mcp_common import _open_db
        from index_mcp_search import _search_fts
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
        from index_mcp_common import _open_db
        from index_mcp_search import _search_fts
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
        from index_mcp_common import _open_db
        from index_mcp_search import _search_fts
        db = _open_db()
        results = _search_fts(db, "indicator", limit=10, file_hint="")
        db.close()
        assert len(results) >= 1
        for r in results:
            name, fpath, _lineno, _stype, _rank = r
            assert fpath in ("a.py", "b.py")
