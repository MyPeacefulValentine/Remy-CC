"""Tests for logic_scope_ui.py SQLite adaptation."""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))
from logic_scope_ui import _open_db, _build_tree_data


def _create_db(claude_dir, files=None, symbols=None):
    db_path = os.path.join(claude_dir, "logic_index.db")
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL, language TEXT, layer TEXT DEFAULT 'Core', imports TEXT)")
    db.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL, name TEXT NOT NULL, short_name TEXT, type TEXT NOT NULL, args TEXT, lineno INTEGER, end_lineno INTEGER, hash TEXT, summary TEXT, bases TEXT, UNIQUE(file_path, name))")
    if files:
        db.executemany("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,?,?,?)", files)
    if symbols:
        db.executemany("INSERT INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, summary, bases) VALUES (?,?,?,?,?,?,?,?,?,?)", symbols)
    db.commit()
    db.close()
    return db_path


@pytest.fixture
def temp_project(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


class TestOpenDb:
    def test_missing_returns_none(self, temp_project):
        result = _open_db(str(temp_project))
        assert result is None

    def test_exists_returns_connection(self, temp_project):
        _create_db(str(temp_project / ".claude"))
        db = _open_db(str(temp_project))
        assert db is not None
        db.close()

    def test_corrupted_returns_none(self, temp_project):
        db_path = temp_project / ".claude" / "logic_index.db"
        db_path.write_bytes(b"not a sqlite database")
        result = _open_db(str(temp_project))
        assert result is None

    def test_custom_project_db_path(self, temp_project):
        custom_dir = temp_project / "state"
        custom_dir.mkdir()
        db_path = _create_db(str(custom_dir))
        (temp_project / ".claude" / "remy-config.json").write_text(json.dumps({
            "schema_version": "1.0.0",
            "values": {"REMY_LOGIC_INDEX_DB_PATH": "state/logic_index.db"},
        }), encoding="utf-8")
        db = _open_db(str(temp_project))
        assert db is not None
        assert os.path.samefile(db_path, db.execute("PRAGMA database_list").fetchone()[2])
        db.close()


class TestBuildTreeData:
    def test_no_db_returns_empty(self, temp_project):
        result = _build_tree_data(str(temp_project))
        assert result["files"] == []
        assert result["layers"] == []
        assert result["selected_files"] is None
        assert result["known_files"] is None

    def test_empty_db_returns_empty(self, temp_project):
        _create_db(str(temp_project / ".claude"))
        result = _build_tree_data(str(temp_project))
        assert result["files"] == []

    def test_with_files_and_symbols(self, temp_project):
        claude_dir = str(temp_project / ".claude")
        _create_db(
            claude_dir,
            files=[
                ("src/main.py", "h1", "PythonParser", "Core", "[]"),
                ("src/utils.py", "h2", "PythonParser", "Util", "[]"),
            ],
            symbols=[
                ("src/main.py", "App", "App", "class", None, 1, 10, None, None, None),
                ("src/main.py", "run", "run", "function", "()", 12, 20, None, None, None),
                ("src/utils.py", "fmt", "fmt", "function", "(s)", 1, 5, None, None, None),
            ],
        )
        result = _build_tree_data(str(temp_project))
        assert len(result["files"]) == 2
        main = next(f for f in result["files"] if f["path"] == "src/main.py")
        assert main["layer"] == "Core"
        assert main["classes"] == 1
        assert main["functions"] == 1
        util = next(f for f in result["files"] if f["path"] == "src/utils.py")
        assert util["functions"] == 1
        assert util["classes"] == 0

    def test_layers_populated(self, temp_project):
        claude_dir = str(temp_project / ".claude")
        _create_db(
            claude_dir,
            files=[("a.py", "h", "P", "Core", "[]"), ("b.py", "h", "P", "Hook", "[]")],
        )
        result = _build_tree_data(str(temp_project))
        layer_names = [l["name"] for l in result["layers"]]
        assert "Core" in layer_names
        assert "Hook" in layer_names

    def test_with_selection(self, temp_project):
        claude_dir = str(temp_project / ".claude")
        _create_db(claude_dir, files=[("x.py", "h", "P", "Core", "[]")])
        sel_path = os.path.join(claude_dir, "logic_inject_selection.json")
        with open(sel_path, "w", encoding="utf-8") as f:
            json.dump({"selected_files": ["x.py"], "known_files": ["x.py", "y.py"]}, f)
        result = _build_tree_data(str(temp_project))
        assert result["selected_files"] == ["x.py"]
        assert result["known_files"] == ["x.py", "y.py"]

    def test_layer_config_ordering(self, temp_project):
        claude_dir = str(temp_project / ".claude")
        config_path = os.path.join(claude_dir, "logic_index_config")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("@layer:Hook=hooks/\n@layer:Core=\n")
        _create_db(
            claude_dir,
            files=[("a.py", "h", "P", "Core", "[]"), ("hooks/b.py", "h", "P", "Hook", "[]")],
        )
        result = _build_tree_data(str(temp_project))
        layer_names = [l["name"] for l in result["layers"]]
        assert layer_names[0] == "Hook"
        assert layer_names[1] == "Core"
