"""Tests for injector.py generate_logic_tree_view (MCP minimal view)."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks", "doc_manager"))
from injector import (
    generate_logic_tree_view,
    _open_logic_db,
    _render_mcp_minimal,
)

SCHEMA_SQL = """
CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL, language TEXT, layer TEXT DEFAULT 'Core', imports TEXT);
CREATE TABLE symbols (id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT NOT NULL, name TEXT NOT NULL, short_name TEXT, type TEXT NOT NULL, args TEXT, lineno INTEGER, end_lineno INTEGER, hash TEXT, summary TEXT, bases TEXT, UNIQUE(file_path, name));
CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT NOT NULL, caller TEXT NOT NULL, callee TEXT NOT NULL, callee_file TEXT, callee_qualified TEXT, line INTEGER, provenance TEXT, synthesized_from TEXT, via TEXT);
CREATE TABLE clusters (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, label TEXT, entry_symbols TEXT NOT NULL, file_count INTEGER);
CREATE TABLE cluster_members (cluster_id INTEGER NOT NULL, file_path TEXT NOT NULL, PRIMARY KEY (cluster_id, file_path));
CREATE TABLE summary_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_kind TEXT NOT NULL,
    node_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    decision_rationale TEXT,
    decision_dimension TEXT,
    decision_confidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(node_kind, node_ref, version)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _setup_db(claude_dir, files=None, symbols=None, clusters=None, cluster_members=None):
    db_path = os.path.join(claude_dir, "logic_index.db")
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO meta VALUES ('version', '4.0.0')")
    db.execute("INSERT INTO meta VALUES ('last_updated', '2026-06-12 21:00:00')")
    if files:
        db.executemany("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,?,?,?)", files)
    if symbols:
        db.executemany("INSERT INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, summary, bases) VALUES (?,?,?,?,?,?,?,?,?,?)", symbols)
        _now = "2025-01-01T00:00:00"
        for sym_row in symbols:
            file_path, sym_name = sym_row[0], sym_row[1]
            sym_summary = sym_row[8]
            if sym_summary:
                payload = json.dumps({"short": sym_summary, "full": None}, ensure_ascii=False)
                db.execute(
                    "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
                    "VALUES ('symbol', ?, 1, ?, 'ok', ?)",
                    (f"{file_path}::{sym_name}", payload, _now)
                )
    if clusters:
        db.executemany("INSERT INTO clusters (name, label, entry_symbols, file_count) VALUES (?,?,?,?)", clusters)
    if cluster_members:
        db.executemany("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?,?)", cluster_members)
    db.commit()
    db.close()
    return db_path


def _inject_cluster_summary(claude_dir, cluster_name, short_value):
    db_path = os.path.join(claude_dir, "logic_index.db")
    db = sqlite3.connect(db_path)
    payload = json.dumps({"short": short_value, "full": None}, ensure_ascii=False)
    db.execute(
        "INSERT INTO summary_versions "
        "(node_kind, node_ref, version, summary, status, created_at) "
        "VALUES ('cluster', ?, 1, ?, 'ok', '2025-01-01T00:00:00')",
        (cluster_name, payload),
    )
    db.commit()
    db.close()


@pytest.fixture
def temp_project(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


class TestOpenLogicDb:
    def test_missing_returns_none(self, temp_project):
        result = _open_logic_db(str(temp_project))
        assert result is None

    def test_exists_returns_connection(self, temp_project):
        _setup_db(str(temp_project / ".claude"))
        db = _open_logic_db(str(temp_project))
        assert db is not None
        db.close()


class TestGenerateLogicTreeView:
    def test_no_db_removes_view(self, temp_project):
        view_path = temp_project / ".claude" / "logic_tree_view.md"
        view_path.write_text("old content", encoding="utf-8")
        generate_logic_tree_view(str(temp_project))
        assert not view_path.exists()

    def test_empty_db_no_output(self, temp_project):
        _setup_db(str(temp_project / ".claude"))
        generate_logic_tree_view(str(temp_project))
        view_path = temp_project / ".claude" / "logic_tree_view.md"
        assert not view_path.exists()

    def test_minimal_view_generated_unconditionally(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("src/app.py", "h1", "PythonParser", "Core", '["src/lib.py"]')],
            symbols=[
                ("src/app.py", "main", "main", "function", "()", 1, 5, None, "Entry point", None),
            ],
            clusters=[("src", "App Core", '["src/app.py::main"]', 1)],
            cluster_members=[(1, "src/app.py")],
        )
        generate_logic_tree_view(str(temp_project))
        view_path = temp_project / ".claude" / "logic_tree_view.md"
        assert view_path.exists()
        content = view_path.read_text(encoding="utf-8")
        assert "逻辑索引" in content
        assert "Files: 1 | Symbols: 1 | Clusters: 1" in content
        assert "query_symbol" in content
        assert "App Core (src)" in content
        assert "### 📄" not in content
        assert "Entry point" not in content


class TestRenderMcpMinimal:
    def _render(self, temp_project, lang="en"):
        db = _open_logic_db(str(temp_project))
        output = []
        _render_mcp_minimal(db, output, lang)
        db.close()
        return "\n".join(output)

    def test_cluster_row_label_with_name_locator(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", "Custom Label", "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        text = self._render(temp_project)
        assert "Custom Label (raw_name)" in text

    def test_cluster_row_uses_summary_short_column(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", None, "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        _inject_cluster_summary(
            str(temp_project / ".claude"), "raw_name", "Subsystem description"
        )
        text = self._render(temp_project)
        assert "raw_name" in text
        assert "Subsystem description" in text

    def test_cluster_row_placeholder_without_summary(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", None, "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        text = self._render(temp_project)
        assert "(no summary)" in text

    def test_zh_placeholder_without_summary(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", None, "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        text = self._render(temp_project, lang="zh-CN")
        assert "(暂无描述)" in text
