"""Tests for struct_scan.py SQLite backend."""

import json
import os
import sqlite3
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import StructScanner, scan_all, scan_files, tokenize_symbol
from index_state import DirtyQueue, StageError
from parsers.base import SymbolInfo
from symbol_selection import (
    DUPLICATE_DEFINITION,
    SIGNATURE_VARIANT,
    TYPE_VARIANT,
    select_symbols,
)


@pytest.fixture
def temp_project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text(
        'def main():\n    """Entry point."""\n    greet("world")\n\ndef greet(name):\n    return f"hello {name}"\n',
        encoding="utf-8",
    )
    (src / "utils.py").write_text(
        'from src.main import greet\n\ndef helper():\n    return greet("test")\n',
        encoding="utf-8",
    )
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    config = claude_dir / "logic_index_config"
    config.write_text("!.git/\n!__pycache__/\n!.claude/\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def scanner(temp_project):
    return StructScanner(str(temp_project))


def _normalized_current_state(db):
    return {
        "files": db.execute(
            "SELECT path,struct_hash,language,layer,imports,kind_hint,actual_kind "
            "FROM files ORDER BY path"
        ).fetchall(),
        "symbols": db.execute(
            "SELECT file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens "
            "FROM symbols ORDER BY file_path,name"
        ).fetchall(),
        "symbol_occurrences": db.execute(
            "SELECT file_path,name,occurrence_index,type,args,lineno,end_lineno,hash,"
            "is_canonical,conflict_kind,selection_reason FROM symbol_occurrences "
            "ORDER BY file_path,name,occurrence_index"
        ).fetchall(),
        "edges": db.execute(
            "SELECT source_file,caller,callee,callee_file,callee_qualified,line,provenance,"
            "synthesized_from,via FROM edges ORDER BY source_file,caller,callee,"
            "callee_qualified,line,provenance,via"
        ).fetchall(),
        "edge_candidates": db.execute(
            "SELECT e.source_file,e.caller,e.callee,e.line,ec.candidate_qualified,ec.score "
            "FROM edge_candidates ec JOIN edges e ON e.id=ec.edge_id "
            "ORDER BY e.source_file,e.caller,e.callee,e.line,ec.candidate_qualified"
        ).fetchall(),
        "patterns": db.execute(
            "SELECT file_path,pattern_type,signal_name,handler,line,metadata FROM patterns "
            "ORDER BY file_path,pattern_type,signal_name,handler,line,metadata"
        ).fetchall(),
        "clusters": db.execute(
            "SELECT name,label,entry_symbols,file_count FROM clusters ORDER BY name"
        ).fetchall(),
        "cluster_members": db.execute(
            "SELECT c.name,cm.file_path FROM cluster_members cm "
            "JOIN clusters c ON c.id=cm.cluster_id ORDER BY c.name,cm.file_path"
        ).fetchall(),
        "retrieval_documents": db.execute(
            "SELECT node_kind,node_ref,language,symbol_type,file_path,name,name_tokens,"
            "signature,summary_short,summary_full,content_hash FROM retrieval_documents "
            "ORDER BY node_kind,node_ref"
        ).fetchall(),
    }


class TestInitDb:
    def test_db_created(self, scanner):
        assert os.path.exists(scanner.db_path)

    def test_schema_tables_exist(self, scanner):
        tables = {r[0] for r in scanner.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = {"files", "symbols", "edges", "edge_candidates", "patterns", "clusters", "cluster_members", "meta"}
        assert expected.issubset(tables)

    def test_version_in_meta(self, scanner):
        row = scanner.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        assert row is not None
        assert row[0] == "10.0.0"

    def test_wal_mode_enabled(self, scanner):
        mode = scanner.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_version_mismatch_without_handler_preserves_db(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n!.claude/\n", encoding="utf-8")
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('version', '4.0.0')")
        db.commit()
        db.close()

        with pytest.raises(RuntimeError, match="Migration path"):
            StructScanner(str(tmp_path))

        db = sqlite3.connect(str(db_path))
        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        assert version == "4.0.0"
        db.close()

    def test_v6_to_v9_ladder_applied(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n!.claude/\n", encoding="utf-8")
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.executescript("""
            CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL,
                                language TEXT, layer TEXT DEFAULT 'Core', imports TEXT);
            CREATE TABLE symbols (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                  file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                                  name TEXT NOT NULL, short_name TEXT, type TEXT NOT NULL,
                                  args TEXT, lineno INTEGER, end_lineno INTEGER,
                                  hash TEXT, summary TEXT, bases TEXT,
                                  name_tokens TEXT NOT NULL DEFAULT '',
                                  UNIQUE(file_path, name));
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE symbols_fts USING fts5(name, name_tokens, file_path, summary,
                                                       content='symbols', content_rowid='id', tokenize='unicode61');
            CREATE TRIGGER symbols_fts_ai AFTER INSERT ON symbols BEGIN
                INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
                VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
            END;
        """)
        db.execute("INSERT INTO meta VALUES ('version', '6.0.0')")
        db.commit()
        db.close()

        scanner = StructScanner(str(tmp_path))
        version = scanner.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        assert version == "10.0.0"
        tables = {r[0] for r in scanner.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "summary_versions" in tables
        assert "retrieval_documents" in tables
        assert "retrieval_fts" in tables
        assert "summary_fts" not in tables
        assert "symbol_occurrences" in tables
        assert "symbols_fts" not in tables
        cols = {c[1] for c in scanner.db.execute("PRAGMA table_info(symbols)").fetchall()}
        assert "summary" not in cols


class TestMigrateJson:
    def test_json_imported(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n", encoding="utf-8")
        old_cache = {
            "_meta": {"version": "3.0.0"},
            "foo.py": {
                "struct_hash": "abc123",
                "language": "PythonParser",
                "layer": "Core",
                "imports": ["bar.py"],
                "symbols": [{"name": "do_stuff", "type": "function", "args": "(x)", "lineno": 1, "end_lineno": 3, "hash": "h1", "summary": "Does stuff"}],
                "calls": [{"caller": "do_stuff", "callee": "helper", "line": 2}],
            },
        }
        json_path = claude_dir / "logic_index.json"
        json_path.write_text(json.dumps(old_cache), encoding="utf-8")

        scanner = StructScanner(str(tmp_path))
        files = scanner.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = scanner.db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        edges = scanner.db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert files == 1
        assert symbols == 1
        assert edges == 1

    def test_json_migrate_populates_name_tokens(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n", encoding="utf-8")
        old_cache = {
            "_meta": {"version": "3.0.0"},
            "mod.py": {
                "struct_hash": "xyz",
                "language": "PythonParser",
                "layer": "Core",
                "imports": [],
                "symbols": [{"name": "getUserById", "type": "function", "args": "(uid)", "lineno": 1, "end_lineno": 3, "hash": "h2", "summary": "Gets user"}],
                "calls": [],
            },
        }
        json_path = claude_dir / "logic_index.json"
        json_path.write_text(json.dumps(old_cache), encoding="utf-8")

        scanner = StructScanner(str(tmp_path))
        row = scanner.db.execute("SELECT name_tokens FROM symbols WHERE name = 'getUserById'").fetchone()
        assert row is not None
        assert row[0] == "get User By Id"

    def test_json_migration_rebuilds_retrieval_projection(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text(
            "!.git/\n!.claude/\n", encoding="utf-8"
        )
        json_path = claude_dir / "logic_index.json"
        json_path.write_text(
            json.dumps({
                "_meta": {"version": "3.0.0"},
                "x.py": {
                    "struct_hash": "a",
                    "language": "PythonParser",
                    "layer": "Core",
                    "imports": [],
                    "symbols": [{
                        "name": "entry",
                        "type": "function",
                        "args": "()",
                        "lineno": 1,
                        "end_lineno": 2,
                        "hash": "h1",
                        "summary": "JSON migration summary",
                    }],
                    "calls": [],
                },
            }),
            encoding="utf-8",
        )

        scanner = StructScanner(str(tmp_path))
        try:
            row = scanner.db.execute(
                "SELECT name, signature, summary_short FROM retrieval_documents "
                "WHERE node_kind='symbol' AND node_ref='x.py::entry'"
            ).fetchone()
            assert row == ("entry", "()", "JSON migration summary")
            match = scanner.db.execute(
                "SELECT d.node_ref FROM retrieval_fts "
                "JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid "
                "WHERE retrieval_fts MATCH 'JSON'"
            ).fetchone()
            assert match == ("x.py::entry",)
        finally:
            scanner.db.close()

    def test_json_renamed_after_migration(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n", encoding="utf-8")
        json_path = claude_dir / "logic_index.json"
        json_path.write_text(json.dumps({"_meta": {"version": "3.0.0"}, "x.py": {"struct_hash": "a", "language": "P", "layer": "Core", "imports": [], "symbols": [], "calls": []}}), encoding="utf-8")

        StructScanner(str(tmp_path))
        assert not json_path.exists()
        assert (claude_dir / "logic_index.json.migrated").exists()


class TestScanFile:
    def test_symbols_inserted(self, scanner, temp_project):
        main_py = str(temp_project / "src" / "main.py")
        from parsers.python_parser import PythonParser
        parser = PythonParser()
        scanner.scan_file(main_py, parser)
        scanner.db.commit()

        count = scanner.db.execute("SELECT COUNT(*) FROM symbols WHERE file_path = 'src/main.py'").fetchone()[0]
        assert count == 2

    def test_edges_inserted(self, scanner, temp_project):
        main_py = str(temp_project / "src" / "main.py")
        from parsers.python_parser import PythonParser
        parser = PythonParser()
        scanner.scan_file(main_py, parser)
        scanner.db.commit()

        edges = scanner.db.execute("SELECT COUNT(*) FROM edges WHERE source_file = 'src/main.py'").fetchone()[0]
        assert edges >= 1

    def test_unchanged_file_skipped(self, scanner, temp_project):
        import json as _json
        main_py = str(temp_project / "src" / "main.py")
        from parsers.python_parser import PythonParser
        parser = PythonParser()
        scanner.scan_file(main_py, parser)
        scanner.db.commit()

        payload = _json.dumps({"short": "cached", "full": None}, ensure_ascii=False)
        scanner.db.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'src/main.py::main', 99, ?, 'ok', '2025-01-01T00:00:00')",
            (payload,)
        )
        scanner.db.commit()

        scanner.scan_file(main_py, parser)
        scanner.db.commit()
        row = scanner.db.execute(
            "SELECT summary FROM summary_versions WHERE node_kind='symbol' "
            "AND node_ref='src/main.py::main' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        decoded = _json.loads(row[0])
        assert decoded["short"] == "cached"

    def test_nonexistent_file_returns_none(self, scanner, temp_project):
        fake_path = str(temp_project / "src" / "nonexistent.py")
        from parsers.python_parser import PythonParser
        result = scanner.scan_file(fake_path, PythonParser())
        assert result is None

    def test_python_duplicate_definitions_are_persisted(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        source_path = tmp_path / "duplicate.py"
        source_path.write_text(
            "def pick():\n    return 1\n\ndef pick():\n    return 2\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            symbol = scanner.db.execute(
                "SELECT name, lineno FROM symbols WHERE file_path='duplicate.py'"
            ).fetchone()
            rows = scanner.db.execute(
                "SELECT occurrence_index, is_canonical, conflict_kind "
                "FROM symbol_occurrences WHERE file_path='duplicate.py' ORDER BY occurrence_index"
            ).fetchall()
            assert symbol == ("pick", 1)
            assert rows == [
                (0, 1, DUPLICATE_DEFINITION),
                (1, 0, DUPLICATE_DEFINITION),
            ]
        finally:
            scanner.db.close()

    def test_c_ifdef_and_cpp_overload_scan_without_conflict(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        (tmp_path / "conditional.c").write_text(
            "#ifdef A\nstatic int pick(void) { return 1; }\n"
            "#else\nstatic int pick(void) { return 2; }\n#endif\n",
            encoding="utf-8",
        )
        (tmp_path / "overload.cpp").write_text(
            "int pick(int value) { return value; }\n"
            "double pick(double value) { return value; }\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            kinds = dict(scanner.db.execute(
                "SELECT file_path, conflict_kind FROM symbol_occurrences "
                "WHERE occurrence_index=0"
            ).fetchall())
            assert kinds["conditional.c"] == DUPLICATE_DEFINITION
            assert kinds["overload.cpp"] == SIGNATURE_VARIANT
        finally:
            scanner.db.close()

    def test_type_variant_is_classified(self):
        symbols = [
            SymbolInfo("Item", "", "struct", 1, "struct Item { int x; };", 1),
            SymbolInfo("Item", "", "typedef", 2, "typedef struct Item Item;", 2),
        ]
        selection = select_symbols(symbols)
        assert {row.conflict_kind for row in selection.occurrences} == {TYPE_VARIANT}
        assert sum(row.is_canonical for row in selection.occurrences) == 1

    def test_selection_is_independent_of_input_order(self):
        symbols = [
            SymbolInfo("pick", "()", "function", 20, "def pick():\n    return 2", 21),
            SymbolInfo("pick", "()", "function", 1, "def pick():\n    return 1\n    return 0", 3),
        ]
        forward = select_symbols(symbols)
        reverse = select_symbols(list(reversed(symbols)))
        assert forward.canonical_symbols[0].lineno == reverse.canonical_symbols[0].lineno
        assert forward.canonical_symbols[0].lineno == 1
        assert forward.occurrences == reverse.occurrences

    def test_occurrences_are_current_state_only(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        source_path = tmp_path / "state.py"
        source_path.write_text(
            "def pick():\n    return 1\n\ndef pick():\n    return 2\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            assert scanner.db.execute(
                "SELECT COUNT(*) FROM symbol_occurrences WHERE file_path='state.py'"
            ).fetchone()[0] == 2
            source_path.write_text("def pick():\n    return 1\n", encoding="utf-8")
            scanner.scan_files(["state.py"])
            assert scanner.db.execute(
                "SELECT COUNT(*) FROM symbol_occurrences WHERE file_path='state.py'"
            ).fetchone()[0] == 0
            source_path.unlink()
            scanner.scan_files(["state.py"])
            assert scanner.db.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_path='state.py'"
            ).fetchone()[0] == 0
        finally:
            scanner.db.close()


    def test_scan_file_failure_does_not_delete_existing_row(self, temp_project, monkeypatch):
        scanner = StructScanner(str(temp_project))
        scanner.scan_all()
        original = scanner.db.execute(
            "SELECT struct_hash FROM files WHERE path='main.py'"
        ).fetchone()
        parser = scanner._get_parser_for_file("main.py")

        def fail_parse(*_args, **_kwargs):
            raise RuntimeError("simulated parser failure")

        monkeypatch.setattr(parser, "parse_symbols", fail_parse)
        (temp_project / "main.py").write_text("def changed():\n    return 2\n", encoding="utf-8")
        result = scanner.scan_all()
        row = scanner.db.execute(
            "SELECT struct_hash FROM files WHERE path='main.py'"
        ).fetchone()
        scanner.db.close()
        assert result.status.value == "partial"
        assert "main.py" in result.failed_paths
        assert row == original

    def test_scan_files_returns_structured_result(self, temp_project):
        scanner = StructScanner(str(temp_project))
        result = scanner.scan_files(["main.py"])
        scanner.db.close()
        assert result.status.value == "success"
        assert result.successful_paths == ("main.py",)
    def test_scan_files_failure_requeues_only_failed_path(self, tmp_path, monkeypatch):
        (tmp_path / ".claude").mkdir()
        for name in ("a.py", "b.py"):
            (tmp_path / name).write_text(f"def {name[0]}():\n    return 1\n", encoding="utf-8")
        scanner = StructScanner(str(tmp_path))
        scanner.scan_all()
        scanner.db.close()
        queue = DirtyQueue(str(tmp_path))
        queue.record("a.py")
        queue.record("b.py")

        original = StructScanner._scan_one_file

        def selective_failure(self, full_path, parser, rel_path):
            if rel_path == "b.py":
                return None, StageError("file_scan", "simulated", rel_path)
            return original(self, full_path, parser, rel_path)

        monkeypatch.setattr(StructScanner, "_scan_one_file", selective_failure)
        result = scan_files(str(tmp_path), ["a.py", "b.py"], manage_dirty=True)
        assert result.status.value == "partial"
        assert queue.peek() == {"b.py"}


class TestResolveCallEdges:
    def test_same_file_priority(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute(
            "SELECT callee_qualified FROM edges WHERE source_file = 'src/main.py' AND callee = 'greet'"
        ).fetchone()
        assert row is not None
        assert row[0] == "src/main.py::greet"

    def test_edge_candidates_created_on_ambiguity(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.git/\n!.claude/\n", encoding="utf-8")
        (tmp_path / "a.py").write_text("from b import parse\nfrom c import parse\n\ndef main():\n    parse()\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def parse():\n    return 'b'\n", encoding="utf-8")
        (tmp_path / "c.py").write_text("def parse():\n    return 'c'\n", encoding="utf-8")

        scanner = StructScanner(str(tmp_path))
        scanner.scan_all()

        candidates = scanner.db.execute("SELECT COUNT(*) FROM edge_candidates").fetchone()[0]
        assert candidates >= 2

    def test_same_file_provenance_is_definite(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute(
            "SELECT provenance FROM edges WHERE source_file = 'src/main.py' AND callee = 'greet'"
        ).fetchone()
        assert row is not None
        assert row[0] == "definite"

    def test_import_resolved_provenance_is_definite(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute(
            "SELECT provenance FROM edges WHERE source_file = 'src/utils.py' AND callee = 'greet'"
        ).fetchone()
        assert row is not None
        assert row[0] == "definite"

    def test_global_unique_provenance_is_probable(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.git/\n!.claude/\n", encoding="utf-8")
        (tmp_path / "a.py").write_text("def caller():\n    unique_fn()\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def unique_fn():\n    pass\n", encoding="utf-8")

        scanner = StructScanner(str(tmp_path))
        scanner.scan_all()

        row = scanner.db.execute(
            "SELECT provenance FROM edges WHERE callee = 'unique_fn'"
        ).fetchone()
        assert row is not None
        assert row[0] == "probable"

    def test_tied_candidates_provenance_is_speculative(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.git/\n!.claude/\n", encoding="utf-8")
        (tmp_path / "a.py").write_text("def caller():\n    dup()\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def dup():\n    return 'b'\n", encoding="utf-8")
        (tmp_path / "c.py").write_text("def dup():\n    return 'c'\n", encoding="utf-8")

        scanner = StructScanner(str(tmp_path))
        scanner.scan_all()

        row = scanner.db.execute(
            "SELECT provenance FROM edges WHERE callee = 'dup'"
        ).fetchone()
        assert row is not None
        assert row[0] == "speculative"


class TestDetectClusters:
    def test_clusters_created(self, scanner, temp_project):
        scanner.scan_all()
        clusters = scanner.db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        assert clusters >= 1

    def test_cluster_members_linked(self, scanner, temp_project):
        scanner.scan_all()
        members = scanner.db.execute("SELECT COUNT(*) FROM cluster_members").fetchone()[0]
        assert members >= 1


class TestScanAll:
    def test_files_populated(self, scanner, temp_project):
        scanner.scan_all()
        count = scanner.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert count == 2

    def test_deleted_files_removed(self, scanner, temp_project):
        scanner.scan_all()
        os.remove(str(temp_project / "src" / "utils.py"))
        scanner.scan_all()
        count = scanner.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert count == 1

    def test_deleted_files_remove_retrieval_projection(self, scanner, temp_project):
        scanner.scan_all()
        symbol_ref = "src/utils.py::helper"
        assert scanner.db.execute(
            "SELECT 1 FROM retrieval_documents WHERE node_ref=?", (symbol_ref,)
        ).fetchone() is not None

        os.remove(str(temp_project / "src" / "utils.py"))
        scanner.scan_all()

        assert scanner.db.execute(
            "SELECT 1 FROM retrieval_documents "
            "WHERE node_ref IN ('src/utils.py', ?)",
            (symbol_ref,),
        ).fetchone() is None
        assert scanner.db.execute(
            "SELECT d.node_ref FROM retrieval_fts "
            "JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid "
            "WHERE d.node_ref IN ('src/utils.py', ?)",
            (symbol_ref,),
        ).fetchone() is None

    def test_meta_updated(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute("SELECT value FROM meta WHERE key='last_updated'").fetchone()
        assert row is not None


class TestResolveGitHead:
    """Cover _resolve_git_head's two-tier strategy: root-as-repo + subdir fallback."""

    @staticmethod
    def _init_repo(path):
        import subprocess
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=str(path), check=True)
        subprocess.run(['git', '-C', str(path), 'config', 'user.email', 't@t.com'], check=True)
        subprocess.run(['git', '-C', str(path), 'config', 'user.name', 'T'], check=True)
        (path / 'seed.txt').write_text('x', encoding='utf-8')
        subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(path), 'commit', '-q', '-m', 'init'], check=True)
        return subprocess.check_output(
            ['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True
        ).strip()

    def test_standard_layout_root_is_repo(self, tmp_path):
        from struct_scan import _resolve_git_head
        expected = self._init_repo(tmp_path)
        head, cwd = _resolve_git_head(str(tmp_path))
        assert head == expected
        assert os.path.realpath(cwd) == os.path.realpath(str(tmp_path))

    def test_subdirectory_fallback_via_db(self, tmp_path):
        from struct_scan import _resolve_git_head
        workspace = tmp_path
        subrepo = workspace / 'project'
        subrepo.mkdir()
        expected = self._init_repo(subrepo)
        db = sqlite3.connect(':memory:')
        db.execute("CREATE TABLE files (path TEXT)")
        db.execute("INSERT INTO files VALUES ('project/seed.txt')")
        head, cwd = _resolve_git_head(str(workspace), db)
        assert head == expected
        assert os.path.realpath(cwd) == os.path.realpath(str(subrepo))

    def test_no_git_anywhere_returns_none_tuple(self, tmp_path):
        from struct_scan import _resolve_git_head
        head, cwd = _resolve_git_head(str(tmp_path))
        assert head is None
        assert cwd is None

    def test_scan_all_writes_source_commit_via_subdir_fallback(self, tmp_path):
        from struct_scan import StructScanner
        workspace = tmp_path
        subrepo = workspace / 'project'
        subrepo.mkdir()
        expected = self._init_repo(subrepo)
        (subrepo / 'a.py').write_text('def x():\n    return 1\n', encoding='utf-8')
        import subprocess
        subprocess.run(['git', '-C', str(subrepo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(subrepo), 'commit', '-q', '-m', 'add a'], check=True)
        head_after = subprocess.check_output(
            ['git', '-C', str(subrepo), 'rev-parse', 'HEAD'], text=True
        ).strip()
        claude_dir = workspace / '.claude'
        claude_dir.mkdir()
        (claude_dir / 'logic_index_config').write_text("!.git/\n", encoding='utf-8')
        scanner = StructScanner(str(workspace))
        scanner.scan_all()
        row = scanner.db.execute(
            "SELECT value FROM meta WHERE key='source_commit'"
        ).fetchone()
        assert row is not None
        assert row[0] == head_after


class TestScanFiles:
    def test_incremental_update(self, scanner, temp_project):
        scanner.scan_all()
        (temp_project / "src" / "main.py").write_text(
            'def main():\n    return 42\n\ndef new_func():\n    pass\n', encoding="utf-8"
        )
        scanner.scan_files(["src/main.py"])
        names = {r[0] for r in scanner.db.execute(
            "SELECT name FROM symbols WHERE file_path = 'src/main.py'"
        ).fetchall()}
        assert "new_func" in names

    def test_incremental_change_marks_old_summary_stale(self, scanner, temp_project):
        scanner.scan_all()
        node_ref = "src/main.py::greet"
        scanner.db.execute(
            "INSERT INTO summary_versions "
            "(node_kind,node_ref,version,summary,status,created_at) "
            "VALUES ('symbol',?,2,?,'ok','2026-01-01')",
            (node_ref, json.dumps({"short": "old semantic text", "full": None})),
        )
        from retrieval_projection import refresh_node
        refresh_node(scanner.db, "symbol", node_ref)
        scanner.db.commit()

        (temp_project / "src" / "main.py").write_text(
            'def main():\n    return 42\n\ndef greet(name):\n    return name.upper()\n',
            encoding="utf-8",
        )
        scanner.scan_files(["src/main.py"])

        status = scanner.db.execute(
            "SELECT status FROM summary_versions "
            "WHERE node_kind='symbol' AND node_ref=? AND version=2",
            (node_ref,),
        ).fetchone()
        assert status == ("stale",)
        document = scanner.db.execute(
            "SELECT summary_short, source_version FROM retrieval_documents "
            "WHERE node_kind='symbol' AND node_ref=?",
            (node_ref,),
        ).fetchone()
        assert document == (None, None)

    def test_incremental_postprocess_failure_rolls_back_and_reports_failed(
        self, scanner, temp_project, monkeypatch
    ):
        scanner.scan_all()
        before = scanner.db.execute(
            "SELECT struct_hash FROM files WHERE path='src/main.py'"
        ).fetchone()
        (temp_project / "src" / "main.py").write_text(
            "def changed():\n    return 2\n", encoding="utf-8"
        )

        def fail_postprocess():
            raise RuntimeError("simulated postprocess failure")

        monkeypatch.setattr(scanner, "_run_postprocess", fail_postprocess)
        result = scanner.scan_files(["src/main.py"])
        after = scanner.db.execute(
            "SELECT struct_hash FROM files WHERE path='src/main.py'"
        ).fetchone()
        assert result.status.value == "failed"
        assert result.successful_paths == ()
        assert result.failed_paths == ("src/main.py",)
        assert after == before

    def test_incremental_rebuilds_synthesized_event_edges(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text(
            "!.claude/\n", encoding="utf-8"
        )
        sender = tmp_path / "sender.py"
        receiver = tmp_path / "receiver.py"
        sender.write_text(
            "def emit_saved():\n    saved.send()\n", encoding="utf-8"
        )
        receiver.write_text(
            "def handle_saved():\n    return 1\n\n"
            "def register():\n    saved.connect(handle_saved)\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            assert scanner.db.execute(
                "SELECT COUNT(*) FROM edges WHERE via='django-signal'"
            ).fetchone()[0] == 1
            receiver.write_text(
                "def handle_saved():\n    return 1\n", encoding="utf-8"
            )
            scanner.scan_files(["receiver.py"])
            assert scanner.db.execute(
                "SELECT COUNT(*) FROM edges WHERE via='django-signal'"
            ).fetchone()[0] == 0
        finally:
            scanner.db.close()

    def test_incremental_matches_fresh_full_scan(self, tmp_path):
        incremental_root = tmp_path / "incremental"
        full_root = tmp_path / "full"
        incremental_root.mkdir()
        full_root.mkdir()
        for root in (incremental_root, full_root):
            (root / ".claude").mkdir()
            (root / ".claude" / "logic_index_config").write_text(
                "!.claude/\n", encoding="utf-8"
            )

        initial = {
            "sender.py": "def emit_saved():\n    saved.send()\n",
            "receiver.py": (
                "def handle_saved():\n    return 1\n\n"
                "def register():\n    saved.connect(handle_saved)\n"
            ),
        }
        final = {
            "sender.py": "def emit_saved():\n    saved.send()\n",
            "receiver.py": (
                "def handle_changed():\n    return 2\n\n"
                "def register():\n    saved.connect(handle_changed)\n"
            ),
            "extra.py": "def helper():\n    return handle_changed()\n",
        }
        for name, source in initial.items():
            (incremental_root / name).write_text(source, encoding="utf-8")
        incremental = StructScanner(str(incremental_root))
        try:
            assert incremental.scan_all().postprocess_complete
            for name, source in final.items():
                (incremental_root / name).write_text(source, encoding="utf-8")
            assert incremental.scan_files(["receiver.py", "extra.py"]).postprocess_complete
            incremental_state = _normalized_current_state(incremental.db)
        finally:
            incremental.db.close()

        for name, source in final.items():
            (full_root / name).write_text(source, encoding="utf-8")
        full = StructScanner(str(full_root))
        try:
            assert full.scan_all().postprocess_complete
            full_state = _normalized_current_state(full.db)
        finally:
            full.db.close()

        assert incremental_state == full_state

    def test_incremental_new_global_name_makes_existing_edge_speculative(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text(
            "!.claude/\n", encoding="utf-8"
        )
        (tmp_path / "caller.py").write_text(
            "def caller():\n    target()\n", encoding="utf-8"
        )
        (tmp_path / "first.py").write_text(
            "def target():\n    return 1\n", encoding="utf-8"
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            before = scanner.db.execute(
                "SELECT provenance,callee_qualified FROM edges "
                "WHERE source_file='caller.py' AND callee='target'"
            ).fetchone()
            assert before == ("probable", "first.py::target")

            (tmp_path / "second.py").write_text(
                "def target():\n    return 2\n", encoding="utf-8"
            )
            scanner.scan_files(["second.py"])
            after = scanner.db.execute(
                "SELECT provenance,callee_qualified FROM edges "
                "WHERE source_file='caller.py' AND callee='target'"
            ).fetchone()
            candidates = scanner.db.execute(
                "SELECT ec.candidate_qualified FROM edge_candidates ec "
                "JOIN edges e ON e.id=ec.edge_id "
                "WHERE e.source_file='caller.py' AND e.callee='target' "
                "ORDER BY ec.candidate_qualified"
            ).fetchall()
            assert after == ("speculative", "first.py::target")
            assert candidates == [("first.py::target",), ("second.py::target",)]
        finally:
            scanner.db.close()

    def test_incremental_change_order_is_commutative(self, tmp_path):
        roots = [tmp_path / "ab", tmp_path / "ba"]
        initial = {
            "caller.py": "def caller():\n    target()\n",
            "first.py": "def target():\n    return 1\n",
        }
        final = {
            "first.py": "def target():\n    return 10\n",
            "second.py": "def target():\n    return 2\n",
        }
        states = []
        for root, order in zip(roots, (["first.py", "second.py"], ["second.py", "first.py"])):
            root.mkdir()
            (root / ".claude").mkdir()
            (root / ".claude" / "logic_index_config").write_text(
                "!.claude/\n", encoding="utf-8"
            )
            for name, source in initial.items():
                (root / name).write_text(source, encoding="utf-8")
            scanner = StructScanner(str(root))
            try:
                scanner.scan_all()
                for name, source in final.items():
                    (root / name).write_text(source, encoding="utf-8")
                assert scanner.scan_files(order).postprocess_complete
                states.append(_normalized_current_state(scanner.db))
            finally:
                scanner.db.close()
        assert states[0] == states[1]

    def test_deleted_conflict_file_removes_occurrences(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        source_path = tmp_path / "duplicate.py"
        source_path.write_text(
            "def pick():\n    return 1\n\ndef pick():\n    return 2\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            source_path.unlink()
            scanner.scan_files(["duplicate.py"])
            scanner.db.commit()
            assert scanner.db.execute(
                "SELECT COUNT(*) FROM symbol_occurrences WHERE file_path='duplicate.py'"
            ).fetchone()[0] == 0
        finally:
            scanner.db.close()


class TestModuleLevelApi:
    def test_scan_all_function(self, temp_project):
        scan_all(str(temp_project))
        db_path = str(temp_project / ".claude" / "logic_index.db")
        assert os.path.exists(db_path)

    def test_scan_files_function(self, temp_project):
        scan_all(str(temp_project))
        scan_files(str(temp_project), ["src/main.py"], manage_dirty=False)
        db = sqlite3.connect(str(temp_project / ".claude" / "logic_index.db"))
        count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        db.close()
        assert count >= 1


class TestTokenizeSymbol:
    def test_snake_case(self):
        assert tokenize_symbol("kmem_cache_alloc_node") == "kmem cache alloc node"

    def test_leading_underscores(self):
        assert tokenize_symbol("__do_page_fault") == "do page fault"

    def test_camel_case(self):
        assert tokenize_symbol("getUserById") == "get User By Id"

    def test_upper_camel_acronym(self):
        assert tokenize_symbol("HTMLResponseWriter") == "HTML Response Writer"

    def test_namespace(self):
        assert tokenize_symbol("std::unordered_map") == "std unordered map"

    def test_mixed_camel(self):
        assert tokenize_symbol("MultiHeadAttention") == "Multi Head Attention"

    def test_empty_string(self):
        assert tokenize_symbol("") == ""

    def test_single_word(self):
        assert tokenize_symbol("main") == "main"

    def test_idempotent(self):
        name = "getUserById"
        assert tokenize_symbol(tokenize_symbol(name)) == tokenize_symbol(name)


class TestFTSSync:
    def test_retrieval_tables_created(self, scanner):
        tables = {r[0] for r in scanner.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "retrieval_documents" in tables
        assert "retrieval_fts" in tables
        assert "summary_fts" not in tables

    def test_name_tokens_populated(self, scanner, temp_project):
        main_py = str(temp_project / "src" / "main.py")
        from parsers.python_parser import PythonParser
        scanner.scan_file(main_py, PythonParser())
        scanner.db.commit()

        row = scanner.db.execute(
            "SELECT name_tokens FROM symbols WHERE file_path = 'src/main.py' AND name = 'main'"
        ).fetchone()
        assert row is not None
        assert row[0] == "main"

    def test_symbol_summary_indexed_via_retrieval_fts(self, scanner, temp_project):
        main_py = str(temp_project / "src" / "main.py")
        from parsers.python_parser import PythonParser
        scanner.scan_file(main_py, PythonParser())
        scanner.db.commit()

        rows = scanner.db.execute(
            "SELECT d.node_kind, d.node_ref FROM retrieval_fts "
            "JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid "
            "WHERE retrieval_fts MATCH 'Entry'"
        ).fetchall()
        assert any(r[0] == "symbol" and r[1] == "src/main.py::main" for r in rows)


class TestComputeKindHint:
    """struct_scan._compute_kind_hint three-tier classification (P0-4)."""

    def test_trivial_below_min_symbols(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(0, 0) == "trivial"
        assert _compute_kind_hint(4, 100) == "trivial"

    def test_low_cohesion_below_density_threshold(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(10, 1) == "low_cohesion"

    def test_cohesive_above_density_threshold(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(10, 5) == "cohesive"

    def test_env_overrides_min_symbols(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.setenv("FILE_KIND_MIN_SYMBOLS", "10")
        monkeypatch.delenv("FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(8, 100) == "trivial"
        assert _compute_kind_hint(10, 5) == "cohesive"

    def test_env_overrides_low_cohesion_threshold(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.setenv("FILE_KIND_LOW_COHESION_THRESHOLD", "0.6")
        assert _compute_kind_hint(10, 5) == "low_cohesion"

    def test_zero_sym_count_with_edges_returns_trivial(self):
        from struct_scan import _compute_kind_hint
        assert _compute_kind_hint(0, 10) == "trivial"

    def test_boundary_at_min_symbols(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(5, 0) == "low_cohesion"
        assert _compute_kind_hint(5, 2) == "cohesive"


class TestDetectClustersCounters:
    """_detect_clusters initializes node_change_counters and prunes stale rows (P1-9)."""

    def test_counter_initialized_for_new_cluster(self, scanner, temp_project):
        scanner.scan_all()
        cluster_names = {
            r[0] for r in scanner.db.execute("SELECT name FROM clusters").fetchall()
        }
        counter_refs = {
            r[0] for r in scanner.db.execute(
                "SELECT node_ref FROM node_change_counters WHERE node_kind='cluster'"
            ).fetchall()
        }
        if cluster_names:
            assert cluster_names.issubset(counter_refs)

    def test_counter_default_zero(self, scanner, temp_project):
        scanner.scan_all()
        rows = scanner.db.execute(
            "SELECT child_change_count, leaf_descendant_count "
            "FROM node_change_counters WHERE node_kind='cluster'"
        ).fetchall()
        for child, leaf in rows:
            assert child == 0
            assert leaf == 0

    def test_stale_cluster_counter_removed(self, scanner, temp_project):
        scanner.scan_all()
        scanner.db.execute(
            "INSERT INTO node_change_counters "
            "(node_kind, node_ref, child_change_count, leaf_descendant_count) "
            "VALUES ('cluster', 'ghost_cluster', 5, 10)"
        )
        scanner.db.commit()
        scanner._detect_clusters()
        rows = scanner.db.execute(
            "SELECT node_ref FROM node_change_counters "
            "WHERE node_kind='cluster' AND node_ref='ghost_cluster'"
        ).fetchall()
        assert rows == []

    def test_counter_preserved_across_rerun(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute("SELECT name FROM clusters LIMIT 1").fetchone()
        if row is None:
            pytest.skip("No clusters detected for this fixture")
        cluster_name = row[0]
        scanner.db.execute(
            "UPDATE node_change_counters SET child_change_count = 42 "
            "WHERE node_kind='cluster' AND node_ref = ?",
            (cluster_name,),
        )
        scanner.db.commit()
        scanner._detect_clusters()
        result = scanner.db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='cluster' AND node_ref = ?",
            (cluster_name,),
        ).fetchone()
        assert result is not None
        assert result[0] == 42

    def test_no_cluster_counter_for_non_cluster_kind(self, scanner, temp_project):
        scanner.scan_all()
        rows = scanner.db.execute(
            "SELECT COUNT(*) FROM node_change_counters WHERE node_kind != 'cluster'"
        ).fetchone()
        assert rows[0] == 0


class TestRunPyV8Compat:
    """run.py LogicIndexer helpers must operate under the current schema."""

    @pytest.fixture
    def indexer(self, tmp_path):
        from struct_scan import SCHEMA_SQL, VERSION
        import run
        db_path = tmp_path / "logic_index.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
        conn.commit()
        instance = run.LogicIndexer.__new__(run.LogicIndexer)
        instance.db = conn
        instance.root_dir = str(tmp_path)
        yield instance
        conn.close()

    def test_symbol_prompt_templates_accept_format_substitution(self, indexer):
        from parsers.c_cpp_parser import CCppParser
        from parsers.python_parser import PythonParser
        from parsers.ts_parser import TSParser

        values = {
            "source_code": "int main(void) { return 0; }",
            "target_symbols": "main",
            "context_summaries": "",
            "lang": "English",
        }
        for parser in (PythonParser(), CCppParser(), TSParser()):
            template = indexer._load_prompt_template(parser)
            rendered = template.format(**values)
            assert "main" in rendered
            assert "int main(void) { return 0; }" in rendered

    def test_c_symbol_prompt_keeps_literal_example_braces(self, indexer):
        from parsers.c_cpp_parser import CCppParser

        template = indexer._load_prompt_template(CCppParser())
        rendered = template.format(
            source_code="source",
            target_symbols="target",
            context_summaries="",
            lang="English",
        )
        assert "void *item) { if (!q" in rendered
        assert "return 0; }" in rendered

    def test_select_dirty_runs_under_v8_without_error(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.commit()
        result = indexer._select_dirty_symbols()
        assert ('a.py', 'foo') in result

    def test_select_dirty_excludes_symbols_with_ok_summary_versions(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'a.py::foo', 1, '{\"short\":\"x\",\"full\":null}', 'ok', '2025-01-01T00:00:00')"
        )
        indexer.db.commit()
        result = indexer._select_dirty_symbols()
        assert ('a.py', 'foo') not in result

    def test_select_dirty_includes_pending_status_rows(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'a.py::foo', 1, NULL, 'pending', '2025-01-01T00:00:00')"
        )
        indexer.db.commit()
        result = indexer._select_dirty_symbols()
        assert ('a.py', 'foo') in result

    def test_persist_creates_first_version_row(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        indexer.db.commit()
        indexer._persist_symbol_summaries([("Parses input", "a.py", "foo")])
        row = indexer.db.execute(
            "SELECT version, status, summary FROM summary_versions "
            "WHERE node_kind='symbol' AND node_ref='a.py::foo'"
        ).fetchone()
        assert row is not None
        version, status, summary_json = row
        assert version == 1
        assert status == "ok"
        assert json.loads(summary_json)["short"] == "Parses input"

    def test_persist_increments_version_on_repeated_writes(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        indexer.db.commit()
        indexer._persist_symbol_summaries([("v1", "a.py", "foo")])
        indexer._persist_symbol_summaries([("v2", "a.py", "foo")])
        versions = [r[0] for r in indexer.db.execute(
            "SELECT version FROM summary_versions WHERE node_ref='a.py::foo' ORDER BY version"
        ).fetchall()]
        assert versions == [1, 2]

    def test_persist_handles_empty_updates(self, indexer):
        indexer._persist_symbol_summaries([])
        count = indexer.db.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0]
        assert count == 0

    def test_dep_context_returns_empty_when_no_imports(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash, imports) VALUES ('a.py', 'h1', '[]')")
        indexer.db.commit()
        assert indexer._get_dep_context_summaries("a.py") == []

    def test_dep_context_fetches_ok_summary_via_summary_versions(self, indexer):
        indexer.db.execute(
            "INSERT INTO files (path, struct_hash, imports) "
            "VALUES ('main.py', 'h1', '[\"lib.py\"]')"
        )
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('lib.py', 'h2')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('lib.py', 'helper', 'function', 'helper')"
        )
        indexer.db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'lib.py::helper', 1, "
            "'{\"short\":\"Helper utility\",\"full\":null}', 'ok', '2025-01-01T00:00:00')"
        )
        indexer.db.commit()
        result = indexer._get_dep_context_summaries("main.py")
        assert ("helper", "Helper utility") in result

    def test_dep_context_skips_pending_status(self, indexer):
        indexer.db.execute(
            "INSERT INTO files (path, struct_hash, imports) "
            "VALUES ('main.py', 'h1', '[\"lib.py\"]')"
        )
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('lib.py', 'h2')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('lib.py', 'helper', 'function', 'helper')"
        )
        indexer.db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'lib.py::helper', 1, NULL, 'pending', '2025-01-01T00:00:00')"
        )
        indexer.db.commit()
        assert indexer._get_dep_context_summaries("main.py") == []

    def test_dep_context_picks_latest_version(self, indexer):
        indexer.db.execute(
            "INSERT INTO files (path, struct_hash, imports) "
            "VALUES ('main.py', 'h1', '[\"lib.py\"]')"
        )
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('lib.py', 'h2')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('lib.py', 'helper', 'function', 'helper')"
        )
        indexer.db.executemany(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'lib.py::helper', ?, ?, 'ok', '2025-01-01T00:00:00')",
            [(1, '{"short":"old","full":null}'), (2, '{"short":"new","full":null}')],
        )
        indexer.db.commit()
        result = indexer._get_dep_context_summaries("main.py")
        assert ("helper", "new") in result
        assert ("helper", "old") not in result

    def test_helpers_end_to_end_under_v8(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.commit()
        dirty_before = indexer._select_dirty_symbols()
        assert ('a.py', 'foo') in dirty_before
        indexer._persist_symbol_summaries([("Test summary", "a.py", "foo")])
        dirty_after = indexer._select_dirty_symbols()
        assert ('a.py', 'foo') not in dirty_after

    def test_helpers_tolerate_missing_db(self, tmp_path):
        import run
        instance = run.LogicIndexer.__new__(run.LogicIndexer)
        instance.db = None
        instance.root_dir = str(tmp_path)
        assert instance._select_dirty_symbols() == []
        assert instance._get_dep_context_summaries("a.py") == []
        instance._persist_symbol_summaries([("x", "a.py", "foo")])

    def test_summary_segment_uses_canonical_selection(self, indexer):
        from parsers.python_parser import PythonParser

        source = "def pick():\n    return 1\n    return 0\n\ndef pick():\n    return 2\n"
        parsed = PythonParser().parse_symbols(source, "a.py")
        selection = select_symbols(parsed)
        segment_map = {
            symbol.name: symbol.source_segment
            for symbol in selection.canonical_symbols
        }
        assert "return 0" in segment_map["pick"]


class TestRunPyHierarchicalBootstrap:
    """LogicIndexer._run_hierarchical_bootstrap consumes SUMMARY_BOOTSTRAP_MODE env
    via bootstrap_summaries and emits marker lines (BOOTSTRAP_RESULT,
    BOOTSTRAP_PENDING_CONFIRMATION) for /remy-index SKILL.md to parse."""

    @pytest.fixture
    def indexer(self, tmp_path):
        from struct_scan import SCHEMA_SQL, VERSION
        import run
        db_path = tmp_path / "logic_index.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
        conn.commit()
        instance = run.LogicIndexer.__new__(run.LogicIndexer)
        instance.db = conn
        instance.root_dir = str(tmp_path)
        instance.api_key = "fake-key"
        instance.circuit_open = False
        instance._call_llm = lambda prompt: '{"short":"x","full":null}'
        yield instance
        conn.close()

    def test_auto_mode_invokes_bootstrap_and_emits_result(self, indexer, monkeypatch, capsys):
        import bootstrap
        recorded = {}

        def fake(db, llm_call, mode=None):
            recorded["mode_arg"] = mode
            return {"mode": "auto", "file_done": 2, "cluster_done": 1, "skipped": False}

        monkeypatch.setattr(bootstrap, "bootstrap_summaries", fake)
        result = indexer._run_hierarchical_bootstrap()
        out = capsys.readouterr().out
        assert "BOOTSTRAP_RESULT mode=auto file_done=2 cluster_done=1" in out
        assert "BOOTSTRAP_PENDING_CONFIRMATION" not in out
        assert result["mode"] == "auto"
        assert recorded["mode_arg"] is None

    def test_ask_mode_emits_pending_confirmation_marker(self, indexer, monkeypatch, capsys):
        import bootstrap

        def fake(db, llm_call, mode=None):
            return {
                "mode": "ask",
                "file_done": 0,
                "cluster_done": 0,
                "skipped": True,
                "needs_user_confirmation": True,
                "pending_files": 3,
                "pending_clusters": 2,
            }

        monkeypatch.setattr(bootstrap, "bootstrap_summaries", fake)
        result = indexer._run_hierarchical_bootstrap()
        out = capsys.readouterr().out
        assert "BOOTSTRAP_RESULT mode=ask" in out
        assert "BOOTSTRAP_PENDING_CONFIRMATION pending_files=3 pending_clusters=2" in out
        assert result["needs_user_confirmation"] is True

    def test_never_mode_records_skip(self, indexer, monkeypatch, capsys):
        import bootstrap

        def fake(db, llm_call, mode=None):
            return {"mode": "never", "file_done": 0, "cluster_done": 0, "skipped": True}

        monkeypatch.setattr(bootstrap, "bootstrap_summaries", fake)
        result = indexer._run_hierarchical_bootstrap()
        out = capsys.readouterr().out
        assert "BOOTSTRAP_RESULT mode=never" in out
        assert "BOOTSTRAP_PENDING_CONFIRMATION" not in out
        assert result["skipped"] is True

    def test_mode_override_passed_through(self, indexer, monkeypatch):
        import bootstrap
        recorded = {}

        def fake(db, llm_call, mode=None):
            recorded["mode_arg"] = mode
            return {"mode": "auto", "file_done": 0, "cluster_done": 0, "skipped": False}

        monkeypatch.setattr(bootstrap, "bootstrap_summaries", fake)
        indexer._run_hierarchical_bootstrap(mode_override="auto")
        assert recorded["mode_arg"] == "auto"

    def test_skipped_when_db_missing(self, tmp_path):
        import run
        instance = run.LogicIndexer.__new__(run.LogicIndexer)
        instance.db = None
        instance.root_dir = str(tmp_path)
        instance.api_key = "fake-key"
        instance.circuit_open = False
        instance._call_llm = lambda p: ""
        result = instance._run_hierarchical_bootstrap()
        assert result is None

    def test_skipped_when_circuit_open(self, indexer):
        indexer.circuit_open = True
        result = indexer._run_hierarchical_bootstrap()
        assert result is None

    def test_warns_and_skips_when_api_key_missing(self, indexer, capsys):
        indexer.api_key = None
        result = indexer._run_hierarchical_bootstrap()
        out = capsys.readouterr().out
        assert result is None
        assert "OPENAI_API_KEY not configured" in out

    def test_bootstrap_exception_caught_and_reported(self, indexer, monkeypatch, capsys):
        import bootstrap

        def fake(db, llm_call, mode=None):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(bootstrap, "bootstrap_summaries", fake)
        result = indexer._run_hierarchical_bootstrap()
        out = capsys.readouterr().out
        assert result is None
        assert "Error during hierarchical bootstrap" in out
        assert "simulated failure" in out

    def test_run_method_triggers_bootstrap_after_symbol_layer(self, indexer, monkeypatch, capsys):
        import bootstrap
        recorded = {"called": False}

        def fake(db, llm_call, mode=None):
            recorded["called"] = True
            return {"mode": "auto", "file_done": 0, "cluster_done": 0, "skipped": False}

        monkeypatch.setattr(bootstrap, "bootstrap_summaries", fake)
        indexer.dirty_nodes = []
        indexer.stats = {"start_time": 0.0, "api_calls": 0}
        try:
            indexer._run_hierarchical_bootstrap()
        finally:
            pass
        assert recorded["called"] is True


class TestPropagationPass:
    """LogicIndexer propagation helpers: force recompute, candidate collection,
    payload build, parent rewrite, and end-to-end pass (P4-C/D/E)."""

    @pytest.fixture
    def indexer(self, tmp_path):
        from struct_scan import SCHEMA_SQL, VERSION
        import run
        db_path = tmp_path / "logic_index.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
        conn.commit()
        instance = run.LogicIndexer.__new__(run.LogicIndexer)
        instance.db = conn
        instance.root_dir = str(tmp_path)
        instance.api_key = "fake-key"
        instance.circuit_open = False
        instance._call_llm = lambda prompt: '{"short":"x","full":null}'
        yield instance
        conn.close()

    def _seed_counter(self, db, kind, ref, child=0, leaf=0, last_force=None):
        db.execute(
            "INSERT INTO node_change_counters "
            "(node_kind, node_ref, child_change_count, leaf_descendant_count, last_force_recompute_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, ref, child, leaf, last_force),
        )
        db.commit()

    def _seed_ok_summary(self, db, kind, ref, short="ok", version=1):
        payload = json.dumps({"short": short, "full": None})
        db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES (?, ?, ?, ?, 'ok', '2025-01-01T00:00:00')",
            (kind, ref, version, payload),
        )
        db.commit()

    def test_force_check_threshold_primary_fires(self, indexer, monkeypatch):
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "3")
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        self._seed_counter(indexer.db, "file", "a.py", child=3)
        assert indexer._force_recompute_check("file", "a.py") is True

    def test_force_check_below_threshold_does_not_fire(self, indexer, monkeypatch):
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "3")
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        self._seed_counter(indexer.db, "file", "a.py", child=2)
        assert indexer._force_recompute_check("file", "a.py") is False

    def test_force_check_backup_disabled_with_negative_one(self, indexer, monkeypatch):
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "1000")
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        self._seed_counter(indexer.db, "file", "a.py", child=0, leaf=99999)
        assert indexer._force_recompute_check("file", "a.py") is False

    def test_force_check_backup_threshold_fires(self, indexer, monkeypatch):
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "1000")
        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_BACKUP", "5")
        self._seed_counter(indexer.db, "file", "a.py", child=0, leaf=5)
        assert indexer._force_recompute_check("file", "a.py") is True

    def test_force_check_no_counter_row_returns_false(self, indexer):
        assert indexer._force_recompute_check("file", "missing.py") is False

    def test_zero_counter_resets_both_fields(self, indexer):
        self._seed_counter(indexer.db, "file", "a.py", child=10, leaf=20)
        indexer._zero_counter("file", "a.py")
        row = indexer.db.execute(
            "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row == (0, 0, None)

    def test_zero_counter_mark_force_stamps_timestamp(self, indexer):
        self._seed_counter(indexer.db, "file", "a.py", child=10, leaf=20)
        indexer._zero_counter("file", "a.py", mark_force=True)
        row = indexer.db.execute(
            "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 0
        assert row[2] is not None

    def test_collect_candidates_includes_parent_with_ok_and_counter(self, indexer):
        self._seed_ok_summary(indexer.db, "file", "a.py")
        self._seed_counter(indexer.db, "file", "a.py", child=2)
        candidates = indexer._collect_propagation_candidates("file")
        assert ("a.py", 2) in candidates

    def test_collect_candidates_excludes_parent_without_ok_summary(self, indexer):
        self._seed_counter(indexer.db, "file", "a.py", child=2)
        candidates = indexer._collect_propagation_candidates("file")
        assert candidates == []

    def test_collect_candidates_excludes_parent_with_zero_counter(self, indexer):
        self._seed_ok_summary(indexer.db, "file", "a.py")
        self._seed_counter(indexer.db, "file", "a.py", child=0)
        candidates = indexer._collect_propagation_candidates("file")
        assert candidates == []

    def test_build_child_changes_file_parent(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.commit()
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="v1", version=1)
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="v2", version=2)
        changes = indexer._build_child_changes_payload("file", "a.py")
        assert len(changes) == 1
        c = changes[0]
        assert c["child_ref"] == "a.py::foo"
        assert c["new_summary"]["short"] == "v2"
        assert c["old_summary"]["short"] == "v1"

    def test_build_child_changes_skips_when_new_equals_old(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.commit()
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="same", version=1)
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="same", version=2)
        changes = indexer._build_child_changes_payload("file", "a.py")
        assert changes == []

    def test_build_child_changes_cluster_parent(self, indexer):
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.execute(
            "INSERT INTO clusters (name, label, entry_symbols, file_count) "
            "VALUES ('C', NULL, '[]', 1)"
        )
        cid = indexer.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        indexer.db.execute(
            "INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'a.py')", (cid,)
        )
        indexer.db.commit()
        self._seed_ok_summary(indexer.db, "file", "a.py", short="fv1", version=1)
        self._seed_ok_summary(indexer.db, "file", "a.py", short="fv2", version=2)
        changes = indexer._build_child_changes_payload("cluster", "C")
        assert len(changes) == 1
        assert changes[0]["new_summary"]["short"] == "fv2"

    def test_rewrite_parent_summary_invokes_summarizer(self, indexer, monkeypatch):
        import summarizer
        calls = {"count": 0}

        def fake_summarize(db, file_path, hint, llm_call):
            calls["count"] += 1
            return {"short": "rewritten", "full": None}, "ok"

        monkeypatch.setattr(summarizer, "summarize_file", fake_summarize)
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.commit()
        indexer._rewrite_parent_summary("file", "a.py")
        assert calls["count"] == 1
        row = indexer.db.execute(
            "SELECT summary FROM summary_versions "
            "WHERE node_kind='file' AND node_ref='a.py' AND status='ok'"
        ).fetchone()
        assert row is not None
        assert json.loads(row[0])["short"] == "rewritten"

    def test_rewrite_parent_skips_pending_status(self, indexer, monkeypatch):
        import summarizer

        def fake_summarize(db, file_path, hint, llm_call):
            return None, "pending"

        monkeypatch.setattr(summarizer, "summarize_file", fake_summarize)
        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.commit()
        indexer._rewrite_parent_summary("file", "a.py")
        row = indexer.db.execute(
            "SELECT COUNT(*) FROM summary_versions "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row[0] == 0

    def test_propagation_pass_propagate_true_rewrites_and_zeros(self, indexer, monkeypatch, capsys):
        import llm_judge
        import summarizer

        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.commit()
        self._seed_ok_summary(indexer.db, "file", "a.py", short="file_v1")
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="s_v1", version=1)
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="s_v2", version=2)
        self._seed_counter(indexer.db, "file", "a.py", child=2)

        monkeypatch.setattr(llm_judge, "judge_propagation",
                            lambda *args, **kw: {"propagate": True, "rationale": "",
                                                 "matched_dimension": "signature",
                                                 "confidence": "high"})
        monkeypatch.setattr(summarizer, "summarize_file",
                            lambda db, fp, hint, llm: ({"short": "file_v2", "full": None}, "ok"))

        stats = indexer._run_propagation_pass()
        assert stats["file_propagate"] == 1
        counter = indexer.db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert counter[0] == 0

    def test_propagation_pass_propagate_false_keeps_counter(self, indexer, monkeypatch):
        import llm_judge

        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        indexer.db.commit()
        self._seed_ok_summary(indexer.db, "file", "a.py", short="file_v1")
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="s_v1", version=1)
        self._seed_ok_summary(indexer.db, "symbol", "a.py::foo", short="s_v2", version=2)
        self._seed_counter(indexer.db, "file", "a.py", child=2)

        monkeypatch.setattr(llm_judge, "judge_propagation",
                            lambda *a, **kw: {"propagate": False, "rationale": "",
                                              "matched_dimension": "internal_refactor",
                                              "confidence": "high"})
        stats = indexer._run_propagation_pass()
        assert stats["file_skip"] == 1
        counter = indexer.db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert counter[0] == 2

    def test_propagation_pass_force_recompute_overrides_verdict(self, indexer, monkeypatch):
        import llm_judge
        import summarizer

        indexer.db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        indexer.db.commit()
        self._seed_ok_summary(indexer.db, "file", "a.py", short="file_v1")
        self._seed_counter(indexer.db, "file", "a.py", child=100)

        monkeypatch.setenv("FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "50")
        judge_called = {"count": 0}

        def fake_judge(*a, **kw):
            judge_called["count"] += 1
            return {"propagate": False}

        monkeypatch.setattr(llm_judge, "judge_propagation", fake_judge)
        monkeypatch.setattr(summarizer, "summarize_file",
                            lambda db, fp, hint, llm: ({"short": "forced", "full": None}, "ok"))

        stats = indexer._run_propagation_pass()
        assert stats["file_force"] == 1
        assert judge_called["count"] == 0
        row = indexer.db.execute(
            "SELECT child_change_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] is not None

    def test_propagation_pass_skipped_when_no_api_key(self, indexer):
        indexer.api_key = None
        result = indexer._run_propagation_pass()
        assert result is None

    def test_propagation_pass_skipped_when_circuit_open(self, indexer):
        indexer.circuit_open = True
        result = indexer._run_propagation_pass()
        assert result is None

