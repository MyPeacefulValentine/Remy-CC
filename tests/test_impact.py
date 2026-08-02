"""Tests for impact.py BFS on SQLite backend."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL
from impact import open_db, get_file_count, collect_file_symbols, bfs_callers, bfs_callees, get_layer, get_line_range


@pytest.fixture
def populated_db(tmp_path):
    db_path = str(tmp_path / ".claude" / "logic_index.db")
    os.makedirs(os.path.dirname(db_path))
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)

    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('a.py','h1','P','Core','[]')")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('b.py','h2','P','Core','[]')")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('c.py','h3','P','Util','[]')")

    db.execute("INSERT INTO symbols (file_path,name,short_name,type,lineno,end_lineno) VALUES ('a.py','main','main','function',1,10)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,lineno,end_lineno) VALUES ('b.py','helper','helper','function',1,5)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,lineno,end_lineno) VALUES ('c.py','util','util','function',1,3)")

    db.execute("INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,line) VALUES ('a.py','main','helper','b.py','b.py::helper',5)")
    db.execute("INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,line) VALUES ('b.py','helper','util','c.py','c.py::util',3)")

    db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
    db.commit()

    os.environ["REMY_LOGIC_INDEX_DB_PATH"] = ".claude/logic_index.db"
    yield db, tmp_path
    db.close()
    if "REMY_LOGIC_INDEX_DB_PATH" in os.environ:
        del os.environ["REMY_LOGIC_INDEX_DB_PATH"]


class TestOpenDb:
    def test_opens_existing_db(self, populated_db):
        db, tmp_path = populated_db
        opened = open_db(str(tmp_path))
        assert opened is not None
        opened.close()

    def test_exits_on_missing_db(self, tmp_path):
        os.environ["REMY_LOGIC_INDEX_DB_PATH"] = ".claude/logic_index.db"
        try:
            with pytest.raises(SystemExit) as exc_info:
                open_db(str(tmp_path))
            assert exc_info.value.code == 2
        finally:
            del os.environ["REMY_LOGIC_INDEX_DB_PATH"]


class TestBfsCallers:
    def test_depth_1(self, populated_db):
        db, _ = populated_db
        levels = bfs_callers(db, {"b.py::helper"}, max_depth=2)
        assert 1 in levels
        assert "a.py::main" in levels[1]

    def test_depth_2(self, populated_db):
        db, _ = populated_db
        levels = bfs_callers(db, {"c.py::util"}, max_depth=2)
        assert 1 in levels
        assert "b.py::helper" in levels[1]
        assert 2 in levels
        assert "a.py::main" in levels[2]

    def test_no_callers(self, populated_db):
        db, _ = populated_db
        levels = bfs_callers(db, {"a.py::main"}, max_depth=3)
        assert levels == {}

    def test_static_only_filter(self, populated_db):
        db, _ = populated_db
        db.execute("INSERT INTO edges (source_file,caller,callee,callee_qualified,line,provenance) VALUES ('c.py','util','main','a.py::main',1,'inferred')")
        db.commit()
        levels_all = bfs_callers(db, {"a.py::main"}, max_depth=1, static_only=False)
        levels_static = bfs_callers(db, {"a.py::main"}, max_depth=1, static_only=True)
        assert len(levels_all.get(1, [])) == 1
        assert len(levels_static.get(1, [])) == 0


class TestBfsCallees:
    def test_depth_1(self, populated_db):
        db, _ = populated_db
        levels = bfs_callees(db, {"a.py::main"}, max_depth=1)
        assert 1 in levels
        assert "b.py::helper" in levels[1]

    def test_chain(self, populated_db):
        db, _ = populated_db
        levels = bfs_callees(db, {"a.py::main"}, max_depth=3)
        assert "b.py::helper" in levels.get(1, [])
        assert "c.py::util" in levels.get(2, [])


class TestHelpers:
    def test_get_file_count(self, populated_db):
        db, _ = populated_db
        assert get_file_count(db) == 3

    def test_collect_file_symbols(self, populated_db):
        db, _ = populated_db
        syms = collect_file_symbols(db, "a.py")
        assert "a.py::main" in syms

    def test_get_layer(self, populated_db):
        db, _ = populated_db
        assert get_layer(db, "c.py") == "Util"
        assert get_layer(db, "nonexistent.py") == "Unknown"

    def test_get_line_range(self, populated_db):
        db, _ = populated_db
        assert get_line_range(db, "a.py::main") == " [L1-L10]"
        assert get_line_range(db, "nonexistent::foo") == ""


class TestBfsChunking:
    """Verify BFS works correctly when current set exceeds chunk size (400)."""

    @pytest.fixture
    def large_db(self, tmp_path):
        db_path = str(tmp_path / ".claude" / "logic_index.db")
        os.makedirs(os.path.dirname(db_path))
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA_SQL)

        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('target.py','h0','P','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,lineno) VALUES ('target.py','target_fn','target_fn','function',1)")

        for i in range(500):
            fname = f"f{i:04d}.py"
            db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,'P','Core','[]')", (fname, f"h{i}"))
            db.execute("INSERT INTO symbols (file_path,name,short_name,type,lineno) VALUES (?,?,?,'function',1)", (fname, f"caller_{i}", f"caller_{i}"))
            db.execute(
                "INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,line) VALUES (?,?,?,?,?,?)",
                (fname, f"caller_{i}", "target_fn", "target.py", "target.py::target_fn", 1)
            )

        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()
        yield db
        db.close()

    def test_bfs_callers_over_400(self, large_db):
        levels = bfs_callers(large_db, {"target.py::target_fn"}, max_depth=1)
        assert 1 in levels
        assert len(levels[1]) == 500

    def test_bfs_callees_over_400(self, large_db):
        all_callers = {f"f{i:04d}.py::caller_{i}" for i in range(500)}
        levels = bfs_callees(large_db, all_callers, max_depth=1)
        assert 1 in levels
        assert "target.py::target_fn" in levels[1]
