"""Tests for logic_enrichment_hook.py SQLite backend."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL


@pytest.fixture
def hook_env(tmp_path):
    db_dir = tmp_path / ".claude"
    db_dir.mkdir()
    db_path = str(db_dir / "logic_index.db")
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)

    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('main.py','h1','PythonParser','Core',?)", (json.dumps(["utils.py"]),))
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('utils.py','h2','PythonParser','Core','[]')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno) VALUES ('main.py','run','run','function','()',1,5)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno) VALUES ('utils.py','helper','helper','function','(x)',1,3)")
    db.execute("INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,line) VALUES ('main.py','run','helper','utils.py','utils.py::helper',3)")
    db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
    db.commit()
    db.close()

    os.environ["LOGIC_INDEX_DB_PATH"] = ".claude/logic_index.db"
    yield tmp_path
    if "LOGIC_INDEX_DB_PATH" in os.environ:
        del os.environ["LOGIC_INDEX_DB_PATH"]


class TestOpenDb:
    def test_returns_connection_when_exists(self, hook_env):
        from logic_enrichment_hook import _open_db
        db = _open_db(str(hook_env))
        assert db is not None
        db.close()

    def test_returns_none_when_missing(self, tmp_path):
        os.environ["LOGIC_INDEX_DB_PATH"] = ".claude/logic_index.db"
        try:
            from logic_enrichment_hook import _open_db
            result = _open_db(str(tmp_path))
            assert result is None
        finally:
            del os.environ["LOGIC_INDEX_DB_PATH"]


class TestBuildEnrichment:
    def test_callees_shown(self, hook_env):
        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("main.py", db)
        db.close()
        assert result is not None
        assert "Calls into:" in result
        assert "utils.py" in result
        assert "helper" in result

    def test_callers_shown(self, hook_env):
        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("utils.py", db)
        db.close()
        assert result is not None
        assert "Called by:" in result
        assert "main.py" in result
        assert "run" in result

    def test_nonexistent_file_returns_none(self, hook_env):
        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("nonexistent.py", db)
        db.close()
        assert result is None

    def test_layer_shown(self, hook_env):
        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("main.py", db)
        db.close()
        assert "(Core)" in result

    def test_imports_fallback(self, hook_env):
        db_path = str(hook_env / ".claude" / "logic_index.db")
        db = sqlite3.connect(db_path)
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('lonely.py','h3','P','Core',?)", (json.dumps(["main.py"]),))
        db.commit()
        db.close()

        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("lonely.py", db)
        db.close()
        assert result is not None
        assert "Imports:" in result


class TestV7SchemaCompatibility:
    """Incremental enrichment hook remains LLM-free under v7 schema (P1-6)."""

    def test_v7_schema_tables_present(self, hook_env):
        db_path = str(hook_env / ".claude" / "logic_index.db")
        db = sqlite3.connect(db_path)
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        db.close()
        assert "summary_versions" in tables
        assert "node_change_counters" in tables
        assert "retrieval_documents" in tables
        assert "retrieval_fts" in tables
        assert "summary_fts" not in tables

    def test_build_enrichment_does_not_invoke_urllib(self, hook_env, monkeypatch):
        import urllib.request

        def forbidden(*args, **kwargs):
            raise AssertionError("Incremental hook must not call LLM API")

        monkeypatch.setattr(urllib.request, "urlopen", forbidden)

        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("main.py", db)
        db.close()
        assert result is not None
        assert "Calls into:" in result

    def test_build_enrichment_with_summary_versions_data(self, hook_env):
        db_path = str(hook_env / ".claude" / "logic_index.db")
        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('file', 'main.py', 1, '{\"short\":\"orchestrator\",\"full\":null}', 'ok', '2025-01-01T00:00:00')"
        )
        db.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'main.py::run', 1, '{\"short\":\"entry point\",\"full\":null}', 'ok', '2025-01-01T00:00:00')"
        )
        db.commit()
        db.close()

        from logic_enrichment_hook import _open_db, _build_enrichment
        db = _open_db(str(hook_env))
        result = _build_enrichment("main.py", db)
        db.close()
        assert result is not None
        assert "Calls into:" in result

    def test_subprocess_call_path_uses_struct_scan_only(self, hook_env, monkeypatch):
        dirty_path = hook_env / ".claude" / "logic_index_dirty"
        dirty_path.write_text("main.py\n", encoding="utf-8")

        recorded = []

        class FakeCompleted:
            returncode = 0

        def fake_run(args, **kwargs):
            recorded.append(args)
            return FakeCompleted()

        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", fake_run)

        from logic_enrichment_hook import _consume_dirty_files
        _consume_dirty_files(str(hook_env), "main.py")

        for args in recorded:
            joined = " ".join(args)
            assert "struct_scan" in joined
            assert "run.py" not in joined

    def test_failed_scan_keeps_dirty_path(self, hook_env, monkeypatch):
        dirty_path = hook_env / ".claude" / "logic_index_dirty"
        dirty_path.write_text("main.py\n", encoding="utf-8")

        class FakeCompleted:
            returncode = 1
            stderr = b"failed"

        original = dirty_path.read_text(encoding="utf-8")
        monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: FakeCompleted())
        from logic_enrichment_hook import _consume_dirty_files
        _consume_dirty_files(str(hook_env), "main.py")
        assert dirty_path.read_text(encoding="utf-8") == original

    def test_partial_scan_keeps_dirty_path(self, hook_env, monkeypatch):
        dirty_path = hook_env / ".claude" / "logic_index_dirty"
        dirty_path.write_text("main.py\n", encoding="utf-8")

        class FakeCompleted:
            returncode = 2
            stderr = b"partial"

        original = dirty_path.read_text(encoding="utf-8")
        monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: FakeCompleted())
        from logic_enrichment_hook import _consume_dirty_files
        _consume_dirty_files(str(hook_env), "main.py")
        assert dirty_path.read_text(encoding="utf-8") == original

    def test_successful_scan_removes_dirty_path(self, hook_env, monkeypatch):
        dirty_path = hook_env / ".claude" / "logic_index_dirty"
        dirty_path.write_text("main.py\n", encoding="utf-8")

        class FakeCompleted:
            returncode = 0
            stderr = b""

        monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: FakeCompleted())
        from logic_enrichment_hook import _consume_dirty_files
        _consume_dirty_files(str(hook_env), "main.py")
        assert dirty_path.read_text(encoding="utf-8") == "main.py\n"
