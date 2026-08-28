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
from index_state import StageError
from parsers.base import ParserCacheIdentity, SymbolInfo
from symbol_selection import (
    DUPLICATE_DEFINITION,
    SIGNATURE_VARIANT,
    TYPE_VARIANT,
    select_symbols,
)


class _StubLlm:
    """In-memory stand-in for llm_client.LlmClient."""

    def __init__(self, response="", api_key: "str | None" = "fake-key"):
        self.response = response
        self.api_key: "str | None" = api_key
        self.circuit_open = False
        self.api_calls = 0
        self.lang = "English"

    def call(self, prompt):
        self.api_calls += 1
        return self.response


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
            "SELECT path,struct_hash,language,layer,imports,kind_hint,actual_kind,"
            "parser_contract_version,parser_backend,parser_environment,import_bindings "
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
            "synthesized_from,via,call_form FROM edges ORDER BY source_file,caller,callee,"
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
        assert row[0] == "12.0.0"

    def test_wal_mode_enabled(self, scanner):
        mode = scanner.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_version_mismatch_is_refused_and_preserves_db(self, tmp_path):
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

        with pytest.raises(RuntimeError, match="remy-daemon scan"):
            StructScanner(str(tmp_path))

        db = sqlite3.connect(str(db_path))
        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        assert version == "4.0.0"
        db.close()

    def test_legacy_v6_database_is_refused(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n!.claude/\n", encoding="utf-8")
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.executescript("""
            CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL,
                                language TEXT, layer TEXT DEFAULT 'Core', imports TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        db.execute("INSERT INTO meta VALUES ('version', '6.0.0')")
        db.commit()
        db.close()

        with pytest.raises(RuntimeError, match="remy-daemon scan"):
            StructScanner(str(tmp_path))

        db = sqlite3.connect(str(db_path))
        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
        assert version == "6.0.0"
        db.close()

    def test_legacy_json_cache_is_ignored_and_left_in_place(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = claude_dir / "logic_index_config"
        config.write_text("!.git/\n!.claude/\n", encoding="utf-8")
        json_path = claude_dir / "logic_index.json"
        json_path.write_text(
            json.dumps({"_meta": {"version": "3.0.0"}, "x.py": {
                "struct_hash": "a", "language": "P", "layer": "Core",
                "imports": [], "symbols": [], "calls": [],
            }}),
            encoding="utf-8",
        )

        scanner = StructScanner(str(tmp_path))
        try:
            assert scanner.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        finally:
            scanner.db.close()
        assert json_path.exists()
        assert not (claude_dir / "logic_index.json.migrated").exists()


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
    def test_scan_files_failure_is_selective(self, tmp_path, monkeypatch):
        (tmp_path / ".claude").mkdir()
        for name in ("a.py", "b.py"):
            (tmp_path / name).write_text(f"def {name[0]}():\n    return 1\n", encoding="utf-8")
        scanner = StructScanner(str(tmp_path))
        scanner.scan_all()
        scanner.db.close()

        original = StructScanner._scan_one_file

        def selective_failure(self, full_path, parser, rel_path):
            if rel_path == "b.py":
                return None, StageError("file_scan", "simulated", rel_path)
            return original(self, full_path, parser, rel_path)

        monkeypatch.setattr(StructScanner, "_scan_one_file", selective_failure)
        result = scan_files(str(tmp_path), ["a.py", "b.py"])
        assert result.status.value == "partial"
        assert result.successful_paths == ("a.py",)
        assert result.failed_paths == ("b.py",)


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

    @staticmethod
    def _project(tmp_path, files):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.git/\n!.claude/\n", encoding="utf-8")
        for rel, content in files.items():
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        scanner = StructScanner(str(tmp_path))
        scanner.scan_all()
        return scanner

    def test_call_form_recorded_for_name_and_attribute(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "def caller(d):\n    plain()\n    d.member()\n",
            "b.py": "def plain():\n    return 1\n",
        })
        rows = dict(scanner.db.execute(
            "SELECT callee, call_form FROM edges WHERE source_file='a.py'"
        ).fetchall())
        assert rows == {"plain": "name", "member": "attribute"}

    def test_attribute_call_global_hit_is_speculative(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "def caller(d):\n    return d.get('k')\n",
            "b.py": "class Cfg:\n    def get(self, key):\n        return key\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges WHERE callee='get'"
        ).fetchone()
        assert row == ("b.py::Cfg.get", "speculative")

    def test_global_tier_skips_cross_language_candidates(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "def caller():\n    return cross_probe()\n",
            "native.c": "int cross_probe(void) {\n    return 0;\n}\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges "
            "WHERE source_file='a.py' AND callee='cross_probe'"
        ).fetchone()
        assert row == (None, None)

    def test_global_tier_same_language_candidate_wins_over_cross_language(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "def caller():\n    return cross_probe()\n",
            "native.c": "int cross_probe(void) {\n    return 0;\n}\n",
            "b.py": "def cross_probe():\n    return 1\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges "
            "WHERE source_file='a.py' AND callee='cross_probe'"
        ).fetchone()
        assert row == ("b.py::cross_probe", "probable")

    def test_attribute_call_import_hit_is_speculative(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "import helper_mod\n\ndef caller():\n    return helper_mod.run()\n",
            "lib/helper_mod.py": "def run():\n    return 'ok'\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges WHERE callee='run'"
        ).fetchone()
        assert row == ("lib/helper_mod.py::run", "speculative")

    def test_same_file_attribute_call_stays_definite(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": (
                "class Own:\n"
                "    def helper(self):\n"
                "        return 1\n"
                "    def call_self(self):\n"
                "        return self.helper()\n"
            ),
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges WHERE callee='helper'"
        ).fetchone()
        assert row == ("a.py::Own.helper", "definite")

    def test_stdlib_binding_suppresses_global_fallback(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": (
                "from unittest.mock import patch\n\n"
                "def caller():\n"
                "    with patch('x'):\n"
                "        return None\n"
            ),
            "b.py": "def patch(target):\n    return target\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges "
            "WHERE source_file='a.py' AND callee='patch'"
        ).fetchone()
        assert row == (None, None)

    def test_nonproject_binding_suppresses_global_fallback(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": (
                "from thirdparty_pkg import parse\n\n"
                "def caller():\n"
                "    return parse('x')\n"
            ),
            "b.py": "def parse(text):\n    return text\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges "
            "WHERE source_file='a.py' AND callee='parse'"
        ).fetchone()
        assert row == (None, None)

    def test_import_binding_supplement_unique_basename_is_definite(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "from helper_mod import run\n\ndef caller():\n    return run()\n",
            "lib/helper_mod.py": "def run():\n    return 'ok'\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges WHERE callee='run'"
        ).fetchone()
        assert row == ("lib/helper_mod.py::run", "definite")

    def test_import_binding_supplement_package_init_is_definite(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "from pkgx import pkfn\n\ndef caller():\n    return pkfn()\n",
            "src/pkgx/__init__.py": "def pkfn():\n    return 'pk'\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges WHERE callee='pkfn'"
        ).fetchone()
        assert row == ("src/pkgx/__init__.py::pkfn", "definite")

    def test_ambiguous_module_name_neither_links_nor_suppresses(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "from helper_mod import run\n\ndef caller():\n    return run()\n",
            "lib/helper_mod.py": "def run():\n    return 'lib'\n",
            "alt/helper_mod.py": "def run():\n    return 'alt'\n",
        })
        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges WHERE callee='run'"
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        assert row[1] == "speculative"

    def test_mixed_call_forms_dedupe_to_name(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "def caller(obj):\n    plain()\n    obj.plain()\n",
            "b.py": "def plain():\n    return 1\n",
        })
        row = scanner.db.execute(
            "SELECT call_form, provenance FROM edges "
            "WHERE source_file='a.py' AND callee='plain'"
        ).fetchone()
        assert row == ("name", "probable")

    def test_incremental_rescan_updates_external_suppression(self, tmp_path):
        scanner = self._project(tmp_path, {
            "a.py": "def caller():\n    return parse('x')\n",
            "b.py": "def parse(text):\n    return text\n",
        })
        row = scanner.db.execute(
            "SELECT provenance FROM edges WHERE source_file='a.py' AND callee='parse'"
        ).fetchone()
        assert row == ("probable",)

        (tmp_path / "a.py").write_text(
            "from thirdparty_pkg import parse\n\ndef caller():\n    return parse('x')\n",
            encoding="utf-8",
        )
        scanner.scan_files(["a.py"])

        row = scanner.db.execute(
            "SELECT callee_qualified, provenance FROM edges "
            "WHERE source_file='a.py' AND callee='parse'"
        ).fetchone()
        assert row == (None, None)

    def test_failed_relative_import_is_not_recorded_as_binding(self, tmp_path):
        from parsers.python_parser import PythonParser
        parser = PythonParser()
        source = "from .missing_mod import thing\n\ndef caller():\n    return thing()\n"
        bindings = parser.collect_import_bindings(
            source, str(tmp_path / "pkg" / "a.py"), str(tmp_path)
        )
        assert bindings == []


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

    def test_parser_cache_identity_is_persisted(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute(
            "SELECT parser_contract_version,parser_backend,parser_environment "
            "FROM files WHERE path='src/main.py'"
        ).fetchone()
        assert row is not None
        assert row[0] == "4"
        assert row[1] == "python-ast"
        assert json.loads(row[2])["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"

    def test_contract_change_reparses_only_matching_parser_family(
        self, scanner, temp_project, monkeypatch
    ):
        scanner.scan_all()
        (temp_project / "entry.ts").write_text(
            "export function entry() { return 1; }\n", encoding="utf-8"
        )
        scanner.scan_files(["entry.ts"])
        python_parser = scanner._get_parser_for_file("main.py")
        ts_parser = scanner._get_parser_for_file("entry.ts")
        calls = []
        original_python = python_parser.parse_symbols
        original_ts = ts_parser.parse_symbols

        monkeypatch.setattr(
            python_parser,
            "cache_identity_candidates",
            lambda _path: (ParserCacheIdentity.create("5", "python-ast", {
                "python": f"{sys.version_info.major}.{sys.version_info.minor}"
            }),),
        )
        monkeypatch.setattr(
            python_parser,
            "cache_identity",
            lambda _source, _path: ParserCacheIdentity.create("5", "python-ast", {
                "python": f"{sys.version_info.major}.{sys.version_info.minor}"
            }),
        )
        monkeypatch.setattr(
            python_parser,
            "parse_symbols",
            lambda source, path: calls.append(path) or original_python(source, path),
        )
        monkeypatch.setattr(
            ts_parser,
            "parse_symbols",
            lambda source, path: pytest.fail(f"unexpected TypeScript parse: {path}"),
        )

        result = scanner.scan_files(["src/main.py"])
        assert result.status.value == "success"
        assert {os.path.basename(path) for path in calls} == {"main.py", "utils.py"}
        rows = scanner.db.execute(
            "SELECT path,parser_contract_version FROM files ORDER BY path"
        ).fetchall()
        assert dict(rows)["entry.ts"] == "3"
        assert dict(rows)["src/main.py"] == "5"
        monkeypatch.setattr(ts_parser, "parse_symbols", original_ts)

    def test_failed_contract_reparse_preserves_old_fact_and_identity(
        self, scanner, temp_project, monkeypatch
    ):
        scanner.scan_all()
        before_file = scanner.db.execute(
            "SELECT struct_hash,parser_contract_version,parser_backend,parser_environment "
            "FROM files WHERE path='src/main.py'"
        ).fetchone()
        before_symbols = scanner.db.execute(
            "SELECT name,hash FROM symbols WHERE file_path='src/main.py' ORDER BY name"
        ).fetchall()
        parser = scanner._get_parser_for_file("main.py")
        environment = json.loads(before_file[3])
        monkeypatch.setattr(
            parser,
            "cache_identity_candidates",
            lambda _path: (ParserCacheIdentity.create("5", "python-ast", environment),),
        )
        monkeypatch.setattr(
            parser,
            "cache_identity",
            lambda _source, _path: ParserCacheIdentity.create("5", "python-ast", environment),
        )
        original_parse = parser.parse_symbols

        def fail_main_only(source, path):
            if path.replace("\\", "/").endswith("src/main.py"):
                raise RuntimeError("parse failed")
            return original_parse(source, path)

        monkeypatch.setattr(parser, "parse_symbols", fail_main_only)

        result = scanner.scan_files(["src/main.py"])
        after_file = scanner.db.execute(
            "SELECT struct_hash,parser_contract_version,parser_backend,parser_environment "
            "FROM files WHERE path='src/main.py'"
        ).fetchone()
        after_symbols = scanner.db.execute(
            "SELECT name,hash FROM symbols WHERE file_path='src/main.py' ORDER BY name"
        ).fetchall()
        assert result.status.value == "partial"
        assert "src/main.py" in result.failed_paths
        assert after_file == before_file
        assert after_symbols == before_symbols

    def test_incremental_exclusion_removes_existing_facts(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        config = tmp_path / ".claude" / "logic_index_config"
        config.write_text("!.claude/\n", encoding="utf-8")
        (tmp_path / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
        excluded = tmp_path / "excluded" / "drop.py"
        excluded.parent.mkdir()
        excluded.write_text("def drop():\n    return 1\n", encoding="utf-8")
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            assert scanner.db.execute(
                "SELECT 1 FROM files WHERE path='excluded/drop.py'"
            ).fetchone() == (1,)
        finally:
            scanner.db.close()

        config.write_text("!.claude/\n!excluded/\n", encoding="utf-8")
        result = scan_files(str(tmp_path), ["excluded/drop.py"])
        assert result.status.value == "success"
        assert result.successful_paths == ("excluded/drop.py",)
        assert result.deleted_paths == ("excluded/drop.py",)
        db = sqlite3.connect(str(tmp_path / ".claude" / "logic_index.db"))
        try:
            for table, column in (
                ("files", "path"),
                ("symbols", "file_path"),
                ("symbol_occurrences", "file_path"),
                ("edges", "source_file"),
                ("patterns", "file_path"),
                ("retrieval_documents", "file_path"),
            ):
                assert db.execute(
                    f"SELECT 1 FROM {table} WHERE {column}='excluded/drop.py' LIMIT 1"
                ).fetchone() is None
        finally:
            db.close()

    def test_double_star_directory_rules_match_root_and_nested_directories(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        config = tmp_path / ".claude" / "logic_index_config"
        config.write_text(
            "!**/.claude/\n!**/__pycache__/\n!**/dist/\n",
            encoding="utf-8",
        )
        roots = (
            tmp_path / ".claude" / "root_hidden.py",
            tmp_path / "__pycache__" / "root_cache.py",
            tmp_path / "dist" / "root_dist.py",
            tmp_path / "pkg" / ".claude" / "nested_hidden.py",
            tmp_path / "pkg" / "__pycache__" / "nested_cache.py",
            tmp_path / "pkg" / "dist" / "nested_dist.py",
        )
        for path in roots:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def excluded():\n    return 1\n", encoding="utf-8")
        (tmp_path / "keep.py").write_text(
            "def keep():\n    return 1\n", encoding="utf-8"
        )

        scanner = StructScanner(str(tmp_path))
        try:
            result = scanner.scan_all()
            assert result.status.value == "success"
            assert scanner.db.execute(
                "SELECT path FROM files ORDER BY path"
            ).fetchall() == [("keep.py",)]
            for path in roots:
                rel = path.relative_to(tmp_path).as_posix()
                assert scanner._is_path_excluded(rel)
        finally:
            scanner.db.close()

    def test_double_star_root_exclusion_removes_existing_unrequested_fact(
        self, tmp_path
    ):
        (tmp_path / ".claude").mkdir()
        config = tmp_path / ".claude" / "logic_index_config"
        config.write_text(
            "# initially include all source directories\n", encoding="utf-8"
        )
        hidden = tmp_path / ".claude" / "temp_log" / "probe.py"
        hidden.parent.mkdir()
        hidden.write_text("def probe():\n    return 1\n", encoding="utf-8")
        (tmp_path / "keep.py").write_text(
            "def keep():\n    return 1\n", encoding="utf-8"
        )
        scanner = StructScanner(str(tmp_path))
        try:
            assert scanner.scan_all().status.value == "success"
            assert scanner.db.execute(
                "SELECT path FROM files WHERE path='.claude/temp_log/probe.py'"
            ).fetchone() == (".claude/temp_log/probe.py",)
        finally:
            scanner.db.close()

        config.write_text("!**/.claude/\n", encoding="utf-8")
        scanner = StructScanner(str(tmp_path))
        try:
            result = scanner.scan_files(["keep.py"])
            assert result.status.value == "success"
            assert result.deleted_paths == (".claude/temp_log/probe.py",)
            assert scanner.db.execute(
                "SELECT path FROM files ORDER BY path"
            ).fetchall() == [("keep.py",)]
        finally:
            scanner.db.close()

    def test_exclusion_globs_clean_unrequested_paths_and_match_fresh_scan(self, tmp_path):
        incremental_root = tmp_path / "incremental_scope"
        fresh_root = tmp_path / "fresh_scope"
        sources = {
            "keep.py": "def keep():\n    return 1\n",
            "root_drop.py": "def root_drop():\n    return 2\n",
            "nested/drop.py": "def nested_drop():\n    return 3\n",
            "nested/keep.py": "def nested_keep():\n    return 4\n",
        }
        for root in (incremental_root, fresh_root):
            (root / ".claude").mkdir(parents=True)
            (root / "nested").mkdir()
            for relative, source in sources.items():
                (root / relative).write_text(source, encoding="utf-8")

        incremental_config = incremental_root / ".claude" / "logic_index_config"
        incremental_config.write_text("!.claude/\n", encoding="utf-8")
        incremental = StructScanner(str(incremental_root))
        try:
            assert incremental.scan_all().status.value == "success"
        finally:
            incremental.db.close()

        rules = "!.claude/\n!root_*.py\n!nested/drop.py\n"
        incremental_config.write_text(rules, encoding="utf-8")
        incremental = StructScanner(str(incremental_root))
        try:
            result = incremental.scan_files(["keep.py"])
            incremental_state = _normalized_current_state(incremental.db)
            assert result.deleted_paths == ("nested/drop.py", "root_drop.py")
            assert result.successful_paths == ("keep.py",)
        finally:
            incremental.db.close()

        (fresh_root / ".claude" / "logic_index_config").write_text(
            rules, encoding="utf-8"
        )
        fresh = StructScanner(str(fresh_root))
        try:
            assert fresh.scan_all().status.value == "success"
            fresh_state = _normalized_current_state(fresh.db)
        finally:
            fresh.db.close()
        assert incremental_state == fresh_state

    def test_mixed_dirty_batch_acknowledges_excluded_and_missing_only(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / ".claude").mkdir()
        config = tmp_path / ".claude" / "logic_index_config"
        config.write_text("!.claude/\n", encoding="utf-8")
        for name in ("valid.py", "excluded.py", "failed.py"):
            (tmp_path / name).write_text(
                f"def {name[:-3]}():\n    return 1\n", encoding="utf-8"
            )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
        finally:
            scanner.db.close()

        config.write_text("!.claude/\n!excluded.py\n", encoding="utf-8")
        original = StructScanner._scan_one_file

        def fail_one(self, full_path, parser, rel_path):
            if rel_path == "failed.py":
                return None, StageError("file_scan", "simulated", rel_path)
            return original(self, full_path, parser, rel_path)

        monkeypatch.setattr(StructScanner, "_scan_one_file", fail_one)
        result = scan_files(
            str(tmp_path),
            ["valid.py", "excluded.py", "missing.py", "failed.py"],
        )
        assert result.status.value == "partial"
        assert result.successful_paths == ("excluded.py", "missing.py", "valid.py")
        assert result.deleted_paths == ("excluded.py",)
        assert result.failed_paths == ("failed.py",)

    @pytest.mark.parametrize(
        ("changed_file", "unchanged_file"),
        (("unit.c", "entry.ts"), ("entry.ts", "unit.c")),
    )
    def test_contract_change_is_selective_for_c_and_typescript(
        self, tmp_path, monkeypatch, changed_file, unchanged_file
    ):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "logic_index_config").write_text(
            "!.claude/\n", encoding="utf-8"
        )
        (tmp_path / "anchor.py").write_text(
            "def anchor():\n    return 1\n", encoding="utf-8"
        )
        (tmp_path / "unit.c").write_text(
            "int unit(void) { return 1; }\n", encoding="utf-8"
        )
        (tmp_path / "entry.ts").write_text(
            "export function entry() { return 1; }\n", encoding="utf-8"
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            changed_parser = scanner._get_parser_for_file(changed_file)
            unchanged_parser = scanner._get_parser_for_file(unchanged_file)
            unchanged_version = scanner.db.execute(
                "SELECT parser_contract_version FROM files WHERE path=?",
                (unchanged_file,),
            ).fetchone()[0]
            row = scanner.db.execute(
                "SELECT parser_backend,parser_environment FROM files WHERE path=?",
                (changed_file,),
            ).fetchone()
            next_identity = ParserCacheIdentity.create(
                "99", row[0], json.loads(row[1])
            )
            calls = []
            original_changed = changed_parser.parse_symbols
            monkeypatch.setattr(
                changed_parser,
                "cache_identity_candidates",
                lambda _path: (next_identity,),
            )
            monkeypatch.setattr(
                changed_parser,
                "cache_identity",
                lambda _source, _path: next_identity,
            )
            monkeypatch.setattr(
                changed_parser,
                "parse_symbols",
                lambda source, path: calls.append(path) or original_changed(source, path),
            )
            monkeypatch.setattr(
                unchanged_parser,
                "parse_symbols",
                lambda _source, path: pytest.fail(f"unexpected parse: {path}"),
            )

            result = scanner.scan_files(["anchor.py"])
            assert result.status.value == "success"
            assert [os.path.basename(path) for path in calls] == [changed_file]
            versions = dict(scanner.db.execute(
                "SELECT path,parser_contract_version FROM files"
            ).fetchall())
            assert versions[changed_file] == "99"
            assert versions[unchanged_file] == unchanged_version
        finally:
            scanner.db.close()

    def test_c_backend_switch_reparses_same_database(self, tmp_path, monkeypatch):
        import parsers.c_cpp_parser as parser_module

        if not parser_module.TREE_SITTER_AVAILABLE:
            pytest.skip("tree-sitter packages are unavailable")
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "logic_index_config").write_text(
            "!.claude/\n", encoding="utf-8"
        )
        (tmp_path / "unit.c").write_text(
            "int unit(void) { return 1; }\n", encoding="utf-8"
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            assert scanner.db.execute(
                "SELECT parser_backend FROM files WHERE path='unit.c'"
            ).fetchone() == ("c-tree-sitter",)

            monkeypatch.setattr(parser_module, "TREE_SITTER_AVAILABLE", False)
            assert scanner.scan_files(["unit.c"]).status.value == "success"
            assert scanner.db.execute(
                "SELECT parser_backend FROM files WHERE path='unit.c'"
            ).fetchone() == ("c-cpp-regex",)

            monkeypatch.setattr(parser_module, "TREE_SITTER_AVAILABLE", True)
            assert scanner.scan_files(["unit.c"]).status.value == "success"
            assert scanner.db.execute(
                "SELECT parser_backend FROM files WHERE path='unit.c'"
            ).fetchone() == ("c-tree-sitter",)
        finally:
            scanner.db.close()

    def test_tree_sitter_metadata_failure_preserves_old_c_facts(
        self, tmp_path, monkeypatch
    ):
        import parsers.c_cpp_parser as parser_module

        if not parser_module.TREE_SITTER_AVAILABLE:
            pytest.skip("tree-sitter packages are unavailable")
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "logic_index_config").write_text(
            "!.claude/\n", encoding="utf-8"
        )
        (tmp_path / "unit.c").write_text(
            "int unit(void) { return 1; }\n", encoding="utf-8"
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            before_file = scanner.db.execute(
                "SELECT struct_hash,parser_contract_version,parser_backend,parser_environment "
                "FROM files WHERE path='unit.c'"
            ).fetchone()
            before_symbols = scanner.db.execute(
                "SELECT name,hash FROM symbols WHERE file_path='unit.c' ORDER BY name"
            ).fetchall()
            monkeypatch.setattr(
                parser_module,
                "distribution_version",
                lambda _name: (_ for _ in ()).throw(RuntimeError("metadata missing")),
            )
            result = scanner.scan_files(["unit.c"])
            assert result.status.value == "failed"
            assert result.failed_paths == ("unit.c",)
            assert scanner.db.execute(
                "SELECT struct_hash,parser_contract_version,parser_backend,parser_environment "
                "FROM files WHERE path='unit.c'"
            ).fetchone() == before_file
            assert scanner.db.execute(
                "SELECT name,hash FROM symbols WHERE file_path='unit.c' ORDER BY name"
            ).fetchall() == before_symbols
        finally:
            scanner.db.close()

    def test_c_header_identity_candidates_cover_actual_grammars(self):
        import parsers.c_cpp_parser as parser_module

        parser = parser_module.CCppParser()
        candidates = parser.cache_identity_candidates("types.h")
        if not parser_module.TREE_SITTER_AVAILABLE:
            assert [identity.backend for identity in candidates] == ["c-cpp-regex"]
            return
        assert {identity.backend for identity in candidates} == {
            "c-tree-sitter", "cpp-tree-sitter"
        }
        assert parser.cache_identity(
            "struct item { int value; };", "types.h"
        ).backend == "c-tree-sitter"
        assert parser.cache_identity(
            "class Item { public: int value; };", "types.h"
        ).backend == "cpp-tree-sitter"

    def test_identity_only_reparse_keeps_symbol_content_hash(
        self, scanner, temp_project, monkeypatch
    ):
        scanner.scan_all()
        node_ref = "src/main.py::main"
        before = scanner.db.execute(
            "SELECT content_hash FROM retrieval_documents "
            "WHERE node_kind='symbol' AND node_ref=?",
            (node_ref,),
        ).fetchone()[0]
        parser = scanner._get_parser_for_file("main.py")
        row = scanner.db.execute(
            "SELECT parser_backend,parser_environment FROM files "
            "WHERE path='src/main.py'"
        ).fetchone()
        identity = ParserCacheIdentity.create("3", row[0], json.loads(row[1]))
        monkeypatch.setattr(
            parser, "cache_identity_candidates", lambda _path: (identity,)
        )
        monkeypatch.setattr(
            parser, "cache_identity", lambda _source, _path: identity
        )
        assert scanner.scan_files(["src/main.py"]).status.value == "success"
        after = scanner.db.execute(
            "SELECT content_hash FROM retrieval_documents "
            "WHERE node_kind='symbol' AND node_ref=?",
            (node_ref,),
        ).fetchone()[0]
        assert after == before

    def test_migrated_empty_identities_retry_failed_file_only(
        self, scanner, temp_project, monkeypatch
    ):
        scanner.scan_all()
        scanner.db.execute(
            "UPDATE files SET parser_contract_version='',parser_backend='',"
            "parser_environment='{}'"
        )
        scanner.db.commit()
        before_main = scanner.db.execute(
            "SELECT name,hash FROM symbols WHERE file_path='src/main.py' ORDER BY name"
        ).fetchall()
        parser = scanner._get_parser_for_file("main.py")
        original_parse = parser.parse_symbols

        def fail_main_only(source, path):
            if path.replace("\\", "/").endswith("src/main.py"):
                raise RuntimeError("parse failed")
            return original_parse(source, path)

        monkeypatch.setattr(parser, "parse_symbols", fail_main_only)
        result = scanner.scan_all()
        assert result.status.value == "partial"
        assert result.failed_paths == ("src/main.py",)
        assert scanner.db.execute(
            "SELECT parser_contract_version FROM files WHERE path='src/main.py'"
        ).fetchone() == ("",)
        assert scanner.db.execute(
            "SELECT parser_contract_version FROM files WHERE path='src/utils.py'"
        ).fetchone() == ("4",)
        assert scanner.db.execute(
            "SELECT name,hash FROM symbols WHERE file_path='src/main.py' ORDER BY name"
        ).fetchall() == before_main

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
        scan_files(str(temp_project), ["src/main.py"])
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
        monkeypatch.delenv("REMY_FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(0, 0) == "trivial"
        assert _compute_kind_hint(4, 100) == "trivial"

    def test_low_cohesion_below_density_threshold(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("REMY_FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(10, 1) == "low_cohesion"

    def test_cohesive_above_density_threshold(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("REMY_FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(10, 5) == "cohesive"

    def test_env_overrides_min_symbols(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.setenv("REMY_FILE_KIND_MIN_SYMBOLS", "10")
        monkeypatch.delenv("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
        assert _compute_kind_hint(8, 100) == "trivial"
        assert _compute_kind_hint(10, 5) == "cohesive"

    def test_env_overrides_low_cohesion_threshold(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("REMY_FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.setenv("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", "0.6")
        assert _compute_kind_hint(10, 5) == "low_cohesion"

    def test_zero_sym_count_with_edges_returns_trivial(self):
        from struct_scan import _compute_kind_hint
        assert _compute_kind_hint(0, 10) == "trivial"

    def test_boundary_at_min_symbols(self, monkeypatch):
        from struct_scan import _compute_kind_hint
        monkeypatch.delenv("REMY_FILE_KIND_MIN_SYMBOLS", raising=False)
        monkeypatch.delenv("REMY_FILE_KIND_LOW_COHESION_THRESHOLD", raising=False)
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
    """LogicIndexer._run_hierarchical_bootstrap consumes REMY_SUMMARY_BOOTSTRAP_MODE
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
        instance.llm_client = _StubLlm('{"short":"x","full":null}')
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
        instance.llm_client = _StubLlm("")
        result = instance._run_hierarchical_bootstrap()
        assert result is None

    def test_skipped_when_circuit_open(self, indexer):
        indexer.llm_client.circuit_open = True
        result = indexer._run_hierarchical_bootstrap()
        assert result is None

    def test_warns_and_skips_when_api_key_missing(self, indexer, capsys):
        indexer.llm_client.api_key = None
        result = indexer._run_hierarchical_bootstrap()
        out = capsys.readouterr().out
        assert result is None
        assert "REMY_LLM_API_KEY not configured" in out

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
        indexer.stats = {"start_time": 0.0}
        try:
            indexer._run_hierarchical_bootstrap()
        finally:
            pass
        assert recorded["called"] is True


class TestSummaryInvalidationScope:
    """scan_file invalidates a file summary only when its symbol set changes.
    Body-only edits keep file and cluster summaries usable so the LLM
    propagation judgment stays reachable."""

    _BASE = (
        'def main():\n    """Entry point."""\n    greet("world")\n\n'
        'def greet(name):\n    return f"hello {name}"\n'
    )

    def _seed(self, db, kind, ref):
        db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES (?, ?, 1, ?, 'ok', '2025-01-01T00:00:00')",
            (kind, ref, json.dumps({"short": "seeded", "full": None})),
        )
        db.commit()

    def _status(self, db, kind, ref):
        row = db.execute(
            "SELECT status FROM summary_versions "
            "WHERE node_kind = ? AND node_ref = ? ORDER BY version DESC LIMIT 1",
            (kind, ref),
        ).fetchone()
        return row[0] if row else None

    def _rescan(self, scanner, temp_project, source):
        (temp_project / "src" / "main.py").write_text(source, encoding="utf-8")
        scanner.scan_files(["src/main.py"])

    def test_body_only_edit_keeps_file_summary_usable(self, scanner, temp_project):
        scanner.scan_all()
        self._seed(scanner.db, "file", "src/main.py")
        self._rescan(scanner, temp_project, self._BASE.replace('"world"', '"universe"'))
        assert self._status(scanner.db, "file", "src/main.py") == "ok"

    def test_added_symbol_invalidates_file_summary(self, scanner, temp_project):
        scanner.scan_all()
        self._seed(scanner.db, "file", "src/main.py")
        self._rescan(
            scanner,
            temp_project,
            self._BASE + '\ndef farewell(name):\n    return f"bye {name}"\n',
        )
        assert self._status(scanner.db, "file", "src/main.py") == "stale"

    def test_removed_symbol_invalidates_file_summary(self, scanner, temp_project):
        scanner.scan_all()
        self._seed(scanner.db, "file", "src/main.py")
        self._rescan(
            scanner,
            temp_project,
            'def main():\n    """Entry point."""\n    return None\n',
        )
        assert self._status(scanner.db, "file", "src/main.py") == "stale"

    def test_body_only_edit_still_invalidates_symbol_summary(self, scanner, temp_project):
        scanner.scan_all()
        self._seed(scanner.db, "symbol", "src/main.py::greet")
        self._rescan(
            scanner,
            temp_project,
            self._BASE.replace('f"hello {name}"', 'f"hi {name}"'),
        )
        assert self._status(scanner.db, "symbol", "src/main.py::greet") == "stale"

    def test_body_only_edit_keeps_cluster_summary_usable(self, scanner, temp_project):
        scanner.scan_all()
        row = scanner.db.execute("SELECT name FROM clusters LIMIT 1").fetchone()
        assert row is not None
        self._seed(scanner.db, "cluster", row[0])
        self._rescan(scanner, temp_project, self._BASE.replace('"world"', '"universe"'))
        assert self._status(scanner.db, "cluster", row[0]) == "ok"


class TestRegistrySharing:
    """LogicIndexer and StructScanner share the same registry instance."""

    def test_indexer_and_scanner_resolve_same_parser_instance(self, temp_project):
        import run
        indexer = run.LogicIndexer(str(temp_project))
        scanner = StructScanner(str(temp_project), registry=indexer._registry)
        py_from_indexer = indexer._get_parser_for_file("main.py")
        py_from_scanner = scanner._get_parser_for_file("main.py")
        assert py_from_indexer is py_from_scanner
        scanner.db.close()

    def test_language_column_preserves_class_name_values(self, scanner, temp_project):
        scanner.scan_all()
        languages = {
            row[0]
            for row in scanner.db.execute("SELECT DISTINCT language FROM files")
        }
        assert languages == {"PythonParser"}



class TestDocstringExcludedFromHash:
    """C2 ruling: the docstring literal never participates in the symbol hash."""

    CASES = [
        (
            "plain",
            'def f(a):\n    """Doc."""\n    return a\n',
            'def f(a):\n    """Changed doc entirely."""\n    return a\n',
            'def f(a):\n    return a + 1\n',
        ),
        (
            "hash_inside_docstring",
            'def f(a):\n    """Doc with # not a comment."""\n    return a\n',
            'def f(a):\n    """Other # text."""\n    return a\n',
            'def f(a):\n    return a - 1\n',
        ),
        (
            "single_quotes_raw",
            "def f(a):\n    r'''Raw\ndoc.'''\n    return a\n",
            'def f(a):\n    """Different style."""\n    return a\n',
            "def f(a):\n    return a * 2\n",
        ),
        (
            "class_docstring",
            'class C:\n    """Class doc."""\n    def m(self):\n        return 1\n',
            'class C:\n    """New class doc."""\n    def m(self):\n        return 1\n',
            'class C:\n    def m(self):\n        return 2\n',
        ),
    ]

    def _scan_hashes(self, tmp_path, name, source):
        project = tmp_path / name
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "logic_index_config").write_text(
            "!.git/\n!.claude/\n", encoding="utf-8")
        (project / "mod.py").write_text(source, encoding="utf-8")
        scanner = StructScanner(str(project))
        try:
            result = scanner.scan_all()
            assert result.status.value == "success", result.errors
            return dict(scanner.db.execute(
                "SELECT name, hash FROM symbols ORDER BY name").fetchall())
        finally:
            scanner.db.close()

    @pytest.mark.parametrize("label,base,doc_edit,body_edit",
                             CASES, ids=[c[0] for c in CASES])
    def test_docstring_only_edit_keeps_hash_body_edit_changes_it(
            self, tmp_path, label, base, doc_edit, body_edit):
        h_base = self._scan_hashes(tmp_path, "base", base)
        h_doc = self._scan_hashes(tmp_path, "doc", doc_edit)
        h_body = self._scan_hashes(tmp_path, "body", body_edit)
        top = sorted(h_base)[0]
        assert h_base[top] == h_doc[top], "docstring-only edit must keep the hash"
        assert h_base[top] != h_body[top], "body edit must change the hash"

    def test_non_docstring_triple_quote_still_hashes(self, tmp_path):
        base = 'def f(a):\n    s = """not a docstring"""\n    return s\n'
        edited = 'def f(a):\n    s = """changed literal"""\n    return s\n'
        h_base = self._scan_hashes(tmp_path, "tq_base", base)
        h_edit = self._scan_hashes(tmp_path, "tq_edit", edited)
        assert h_base["f"] != h_edit["f"], (
            "a triple-quoted assignment value is not a docstring "
            "and must stay inside the hash")

    def test_no_docstring_symbol_hash_unchanged_by_ruling(self, tmp_path):
        source = 'def f(a):\n    return a\n'
        from parsers.python_parser import PythonParser
        parser = PythonParser()
        symbols = parser.parse_symbols(source, "mod.py")
        assert symbols[0].hash_source_segment is None
        assert symbols[0].hash_segment() == symbols[0].source_segment

    def test_hash_segment_splices_exact_docstring_span(self):
        from parsers.python_parser import PythonParser
        source = 'def f(a):\n    """Doc."""\n    return a  # tail\n'
        parser = PythonParser()
        symbols = parser.parse_symbols(source, "mod.py")
        seg = symbols[0].hash_segment()
        assert '"""Doc."""' not in seg
        assert "return a" in seg
        assert seg != symbols[0].source_segment

    def test_source_segment_stays_complete_for_summaries(self):
        from parsers.python_parser import PythonParser
        source = 'def f(a):\n    """Doc."""\n    return a\n'
        parser = PythonParser()
        symbols = parser.parse_symbols(source, "mod.py")
        assert '"""Doc."""' in symbols[0].source_segment
        assert symbols[0].docstring == "Doc."

    def test_fstring_and_bytes_first_statements_are_not_docstrings(self):
        from parsers.python_parser import PythonParser
        parser = PythonParser()
        for source in (
            'def f(a):\n    f"""formatted {a}"""\n    return a\n',
            'def f(a):\n    b"""bytes literal"""\n    return a\n',
        ):
            symbols = parser.parse_symbols(source, "mod.py")
            assert symbols[0].hash_source_segment is None, source

    def test_contract_v2_rows_migrate_once_then_scan_is_idempotent(self, tmp_path, monkeypatch):
        project = tmp_path / "migrate"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "logic_index_config").write_text(
            "!.git/\n!.claude/\n", encoding="utf-8")
        (project / "mod.py").write_text(
            'def f(a):\n    """Doc."""\n    return a\n', encoding="utf-8")
        scanner = StructScanner(str(project))
        try:
            assert scanner.scan_all().status.value == "success"
            scanner.db.execute("UPDATE files SET parser_contract_version='2'")
            scanner.db.commit()

            parser = scanner._get_parser_for_file("mod.py")
            calls = []
            original_parse = parser.parse_symbols
            monkeypatch.setattr(
                parser,
                "parse_symbols",
                lambda source, path: calls.append(path) or original_parse(source, path),
            )

            assert scanner.scan_all().status.value == "success"
            assert calls, "a stored v2 row must be re-parsed"
            migrated_call_count = len(calls)
            versions = {row[0] for row in scanner.db.execute(
                "SELECT parser_contract_version FROM files")}
            assert versions == {"4"}

            assert scanner.scan_all().status.value == "success"
            assert len(calls) == migrated_call_count, "a migrated row must not re-parse again"
        finally:
            scanner.db.close()
