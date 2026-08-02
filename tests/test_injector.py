"""Tests for injector.py generate_logic_tree_view and rendering functions."""

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks", "doc_manager"))
from injector import (
    generate_logic_tree_view,
    _get_injection_density,
    _open_logic_db,
    _render_full,
    _render_cluster,
    _render_cluster_summary,
    _meta_line,
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

ICON_MAP = {
    "class": "C", "function": "f", "struct": "S", "enum": "E",
    "typedef": "T", "type_alias": "T", "macro": "M",
    "namespace": "N", "interface": "I",
}


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


@pytest.fixture
def temp_project(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


class TestGetInjectionDensity:
    def test_small_project_full(self):
        assert _get_injection_density(50) == "full"

    def test_medium_project_cluster(self):
        assert _get_injection_density(500) == "cluster"

    def test_large_project_summary(self):
        assert _get_injection_density(3000) == "cluster_summary"

    def test_boundary_full_max(self):
        assert _get_injection_density(200) == "full"
        assert _get_injection_density(201) == "cluster"

    def test_boundary_cluster_max(self):
        assert _get_injection_density(2000) == "cluster"
        assert _get_injection_density(2001) == "cluster_summary"


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

    def test_full_mode_generates_output(self, temp_project, monkeypatch):
        monkeypatch.setenv("REMY_NAV_MCP_MINIMAL_ENABLED", "false")
        _setup_db(
            str(temp_project / ".claude"),
            files=[("src/app.py", "h1", "PythonParser", "Core", '["src/lib.py"]')],
            symbols=[
                ("src/app.py", "main", "main", "function", "()", 1, 5, None, "Entry point", None),
                ("src/app.py", "Config", "Config", "class", None, 7, 20, None, "App config", None),
            ],
        )
        generate_logic_tree_view(str(temp_project))
        view_path = temp_project / ".claude" / "logic_tree_view.md"
        assert view_path.exists()
        content = view_path.read_text(encoding="utf-8")
        assert "逻辑索引" in content
        assert "`main()`" in content
        assert "`Config`" in content
        assert "Entry point" in content
        assert "src/lib.py" in content


class TestRenderFull:
    def test_basic_output(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            symbols=[("a.py", "foo", "foo", "function", "(x)", 1, 3, None, "Does foo", None)],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_full(db, output, None, 1, "en", ICON_MAP)
        db.close()
        text = "\n".join(output)
        assert "Core" in text
        assert "`a.py`" in text
        assert "`foo(x)`" in text
        assert "Does foo" in text

    def test_selection_filters(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]"), ("b.py", "h", "P", "Core", "[]")],
            symbols=[
                ("a.py", "fa", "fa", "function", "()", 1, 2, None, None, None),
                ("b.py", "fb", "fb", "function", "()", 1, 2, None, None, None),
            ],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_full(db, output, {"a.py"}, 2, "en", ICON_MAP)
        db.close()
        text = "\n".join(output)
        assert "`a.py`" in text
        assert "`b.py`" not in text


class TestRenderCluster:
    def test_with_clusters(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("hooks/guard.py", "h", "P", "Hook", "[]")],
            symbols=[("hooks/guard.py", "main", "main", "function", "()", 1, 5, None, "Guard entry", None)],
            clusters=[("hooks", "Hook System", '["hooks/guard.py::main"]', 1)],
            cluster_members=[(1, "hooks/guard.py")],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster(db, output, None, 1, "en", ICON_MAP)
        db.close()
        text = "\n".join(output)
        assert "Hook System" in text
        assert "`main()`" in text

    def test_no_clusters_fallback_to_full(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("x.py", "h", "P", "Core", "[]")],
            symbols=[("x.py", "f", "f", "function", "()", 1, 2, None, None, None)],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster(db, output, None, 1, "en", ICON_MAP)
        db.close()
        text = "\n".join(output)
        assert "`x.py`" in text


class TestRenderClusterSummary:
    def test_basic_output(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            clusters=[
                ("parsers", "Parser Engine", '["parsers/base.py::parse"]', 5),
                ("hooks", None, '["hooks/main.py::run"]', 3),
            ],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster_summary(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "Parser Engine" in text
        assert "(5 files)" in text
        assert "hooks" in text
        assert "(3 files)" in text

    def test_no_clusters(self, temp_project):
        _setup_db(str(temp_project / ".claude"))
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster_summary(db, output, "en")
        db.close()
        assert any("No clusters" in line for line in output)


class TestMetaLine:
    def test_chinese(self):
        result = _meta_line("zh-CN", 10, 50)
        assert "10/50" in result

    def test_english(self):
        result = _meta_line("en", 10, 50)
        assert "10/50" in result


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


class TestClusterDisplayFallback:
    """display = label or summary.short or name in _render_cluster_summary (P1-5)."""

    def test_label_only_displays_label(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", "Custom Label", "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster_summary(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "Custom Label (raw_name)" in text

    def test_summary_short_used_when_label_empty(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", None, "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        _inject_cluster_summary(
            str(temp_project / ".claude"), "raw_name", "Subsystem description"
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster_summary(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "Subsystem description" in text

    def test_name_used_when_label_and_summary_empty(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", None, "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster_summary(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "raw_name" in text

    def test_label_and_summary_both_present_suffix(self, temp_project):
        _setup_db(
            str(temp_project / ".claude"),
            files=[("a.py", "h", "P", "Core", "[]")],
            clusters=[("raw_name", "Display Name", "[]", 1)],
            cluster_members=[(1, "a.py")],
        )
        _inject_cluster_summary(
            str(temp_project / ".claude"), "raw_name", "Detailed description"
        )
        db = _open_logic_db(str(temp_project))
        output = []
        _render_cluster_summary(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "Display Name" in text
        assert "Detailed description" in text
        assert "Display Name" in text.split("Detailed description")[0]
