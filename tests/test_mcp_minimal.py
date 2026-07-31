import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks" / "doc_manager"))
from injector import _detect_mcp_available, _render_mcp_minimal


class TestDetectMcpAvailable:
    def test_script_missing_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        with patch("injector.os.path.expanduser", return_value=str(tmp_path)):
            assert _detect_mcp_available() is False

    def test_sdk_missing_returns_false(self, tmp_path, monkeypatch):
        script_dir = tmp_path / ".claude" / "remy-src"
        script_dir.mkdir(parents=True)
        (script_dir / "index_mcp_server.py").write_text("")
        with patch("injector.os.path.expanduser", return_value=str(tmp_path)):
            with patch("importlib.util.find_spec", return_value=None):
                assert _detect_mcp_available() is False

    def test_both_present_returns_true(self, tmp_path, monkeypatch):
        script_dir = tmp_path / ".claude" / "remy-src"
        script_dir.mkdir(parents=True)
        (script_dir / "index_mcp_server.py").write_text("")
        with patch("injector.os.path.expanduser", return_value=str(tmp_path)):
            with patch("importlib.util.find_spec", return_value=object()):
                assert _detect_mcp_available() is True


def _create_db(path, clusters=None, files_count=5, symbols_count=20):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL, language TEXT, layer TEXT DEFAULT 'Core', imports TEXT, kind_hint TEXT, actual_kind TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS symbols (file_path TEXT, name TEXT, qualified TEXT, type TEXT, args TEXT, lineno INT, end_lineno INT, source_hash TEXT, bases TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS clusters (id INTEGER PRIMARY KEY, name TEXT, label TEXT, entry_symbols TEXT, file_count INT)")
    db.execute("CREATE TABLE IF NOT EXISTS summary_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, node_kind TEXT NOT NULL, node_ref TEXT NOT NULL, version INTEGER NOT NULL, summary TEXT, status TEXT NOT NULL DEFAULT 'ok', decision_rationale TEXT, decision_dimension TEXT, decision_confidence TEXT, created_at TEXT NOT NULL, UNIQUE(node_kind, node_ref, version))")
    db.execute("INSERT INTO meta VALUES ('last_updated', '2026-06-13 03:00:00')")
    for i in range(files_count):
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?, 'h', 'P', 'Core', '[]')", (f"src/f{i}.py",))
    for i in range(symbols_count):
        db.execute("INSERT INTO symbols VALUES (?, ?, ?, 'function', '()', ?, ?, NULL, NULL)",
                   (f"src/f{i % files_count}.py", f"fn{i}", f"src/f{i % files_count}.py::fn{i}", i+1, i+5))
    if clusters:
        for cid, (name, label, entries, fc) in enumerate(clusters):
            db.execute("INSERT INTO clusters VALUES (?, ?, ?, ?, ?)",
                       (cid, name, label, json.dumps(entries), fc))
    db.commit()
    return db


class TestRenderMcpMinimal:
    def test_no_clusters(self, tmp_path):
        db = _create_db(str(tmp_path / "test.db"), clusters=None)
        output = []
        _render_mcp_minimal(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "Files: 5" in text
        assert "Symbols: 20" in text
        assert "Cluster Overview" not in text

    def test_with_clusters_zh(self, tmp_path):
        clusters = [
            ("core", "核心模块", ["src/f0.py::fn0", "src/f1.py::fn1"], 3),
            ("api", "API 层", ["src/f2.py::fn2"], 2),
        ]
        db = _create_db(str(tmp_path / "test.db"), clusters=clusters)
        output = []
        _render_mcp_minimal(db, output, "zh-CN")
        db.close()
        text = "\n".join(output)
        assert "集群概览" in text
        assert "核心模块" in text
        assert "描述" in text
        assert "使用时机" in text

    def test_with_clusters_en(self, tmp_path):
        clusters = [
            ("core", "Core", ["src/f0.py::fn0"], 3),
        ]
        db = _create_db(str(tmp_path / "test.db"), clusters=clusters)
        output = []
        _render_mcp_minimal(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "Cluster Overview" in text
        assert "When to use" in text
        assert "query_symbol" in text

    def test_template_missing_fallback(self, tmp_path):
        db = _create_db(str(tmp_path / "test.db"))
        output = []
        with patch("injector.os.path.dirname", return_value=str(tmp_path)):
            _render_mcp_minimal(db, output, "en")
        db.close()
        text = "\n".join(output)
        assert "MCP available" in text
