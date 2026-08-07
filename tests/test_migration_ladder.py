"""Tests for v6 -> v7 migration ladder in struct_scan.py."""

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
migrations = importlib.import_module("migrations")
schema = importlib.import_module("schema")

MIGRATION_HANDLERS = migrations.MIGRATION_HANDLERS
_migrate_v6_to_v7 = migrations._migrate_v6_to_v7
_migrate_v7_to_v8 = migrations._migrate_v7_to_v8
_migrate_v8_to_v9 = migrations._migrate_v8_to_v9
_migrate_v9_to_v10 = migrations._migrate_v9_to_v10
_migrate_v10_to_v11 = migrations._migrate_v10_to_v11
_migrate_v11_to_v12 = migrations._migrate_v11_to_v12
_resolve_migration_path = migrations._resolve_migration_path
SCHEMA_SQL = schema.SCHEMA_SQL
VERSION = schema.VERSION


V6_SCHEMA = """
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name TEXT NOT NULL,
    short_name TEXT,
    type TEXT NOT NULL,
    args TEXT,
    lineno INTEGER,
    end_lineno INTEGER,
    hash TEXT,
    summary TEXT,
    bases TEXT,
    name_tokens TEXT NOT NULL DEFAULT '',
    UNIQUE(file_path, name)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    name, name_tokens, file_path, summary,
    content='symbols', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER symbols_fts_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
    VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
END;
CREATE TRIGGER symbols_fts_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, name_tokens, file_path, summary)
    VALUES ('delete', OLD.id, OLD.name, OLD.name_tokens, OLD.file_path, OLD.summary);
END;
CREATE TRIGGER symbols_fts_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, name_tokens, file_path, summary)
    VALUES ('delete', OLD.id, OLD.name, OLD.name_tokens, OLD.file_path, OLD.summary);
    INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
    VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
END;
"""


def _make_v6_db(path):
    db = sqlite3.connect(str(path))
    db.executescript(V6_SCHEMA)
    db.execute("INSERT INTO meta (key, value) VALUES ('version', '6.0.0')")
    db.execute(
        "INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,?,?,?)",
        ("src/foo.py", "h1", "PythonParser", "Core", "[]"),
    )
    db.execute(
        "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, summary, name_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("src/foo.py", "alpha", "alpha", "function", "()", 1, 5, "sh1", "Computes alpha", "alpha"),
    )
    db.commit()
    return db


def _make_v7_db(path):
    db = _make_v6_db(path)
    _migrate_v6_to_v7(db)
    db.execute("UPDATE meta SET value='7.0.0' WHERE key='version'")
    db.execute("UPDATE files SET struct_hash='h1'")
    db.commit()
    return db


def test_migrate_v6_to_v7_creates_new_tables(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    _migrate_v6_to_v7(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "summary_versions" in tables
    assert "node_change_counters" in tables
    assert "judge_cache" in tables
    assert "migration_log" in tables
    assert "summary_fts" in tables
    assert "symbols_fts" not in tables
    db.close()


def test_migrate_preserves_symbol_summaries(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    _migrate_v6_to_v7(db)
    row = db.execute(
        "SELECT summary, status, version FROM summary_versions "
        "WHERE node_kind='symbol' AND node_ref='src/foo.py::alpha'"
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["short"] == "Computes alpha"
    assert payload["full"] is None
    assert row[1] == "ok"
    assert row[2] == 1
    db.close()


def test_migrate_drops_symbols_summary_column(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    _migrate_v6_to_v7(db)
    cols = {c[1] for c in db.execute("PRAGMA table_info(symbols)").fetchall()}
    assert "summary" not in cols
    db.close()


def test_migrate_adds_kind_hint_columns(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    _migrate_v6_to_v7(db)
    cols = {c[1] for c in db.execute("PRAGMA table_info(files)").fetchall()}
    assert "kind_hint" in cols
    assert "actual_kind" in cols
    db.close()


def test_migrate_records_log(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    _migrate_v6_to_v7(db)
    rows = db.execute("SELECT from_version, to_version FROM migration_log").fetchall()
    assert ("6.0.0", "7.0.0") in rows
    db.close()


class _FailOnMatch:
    """Test wrapper raising IntegrityError on the first execute() whose SQL contains the marker substring."""

    def __init__(self, real_db, marker):
        self._real = real_db
        self._marker = marker
        self._fired = False

    def execute(self, sql, *params):
        if not self._fired and self._marker in sql:
            self._fired = True
            raise sqlite3.IntegrityError(f"simulated constraint failure on: {sql.strip()[:80]}")
        if params:
            return self._real.execute(sql, *params)
        return self._real.execute(sql)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_migrate_rollback_on_failure(tmp_path):
    db_path = tmp_path / "logic_index.db"
    real_db = _make_v6_db(db_path)
    wrapper = _FailOnMatch(real_db, "ALTER TABLE symbols DROP COLUMN")
    with pytest.raises(sqlite3.IntegrityError):
        _migrate_v6_to_v7(wrapper)

    file_cols = {c[1] for c in real_db.execute("PRAGMA table_info(files)").fetchall()}
    assert "kind_hint" not in file_cols
    assert "actual_kind" not in file_cols

    sym_cols = {c[1] for c in real_db.execute("PRAGMA table_info(symbols)").fetchall()}
    assert "summary" in sym_cols

    tables = {r[0] for r in real_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "symbols_fts" in tables

    if "summary_versions" in tables:
        sv_count = real_db.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0]
        assert sv_count == 0

    if "migration_log" in tables:
        log_count = real_db.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]
        assert log_count == 0

    row = real_db.execute("SELECT summary FROM symbols WHERE name='alpha'").fetchone()
    assert row is not None
    assert row[0] == "Computes alpha"
    real_db.close()


def test_resolve_migration_path_direct():
    chain = _resolve_migration_path("10.0.0", "11.0.0")
    assert chain is not None
    assert len(chain) == 1
    assert chain[0][0] == "10.0.0"
    assert chain[0][1] == "11.0.0"


def test_resolve_migration_path_chained():
    chain = _resolve_migration_path("6.0.0", "11.0.0")
    assert chain is not None
    assert [(step[0], step[1]) for step in chain] == [
        ("6.0.0", "7.0.0"),
        ("7.0.0", "8.0.0"),
        ("8.0.0", "9.0.0"),
        ("9.0.0", "10.0.0"),
        ("10.0.0", "11.0.0"),
    ]


def test_resolve_migration_path_missing_returns_none():
    chain = _resolve_migration_path("3.0.0", "8.0.0")
    assert chain is None


def test_migration_handlers_registered():
    expected = (
        ("6.0.0", "7.0.0"),
        ("7.0.0", "8.0.0"),
        ("8.0.0", "9.0.0"),
        ("9.0.0", "10.0.0"),
        ("10.0.0", "11.0.0"),
        ("11.0.0", "12.0.0"),
    )
    for step in expected:
        assert step in MIGRATION_HANDLERS
        assert callable(MIGRATION_HANDLERS[step])


def test_version_constant_is_v12():
    assert VERSION == "12.0.0"


def test_migration_modules_import_without_parsers():
    skill_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index")
    )
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        skill_dir + os.pathsep + existing_pythonpath
        if existing_pythonpath else skill_dir
    )
    script = (
        "import importlib, json, sys; "
        "importlib.import_module('schema'); "
        "importlib.import_module('migrations'); "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name == 'parsers' or name.startswith('parsers.'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        env=env,
        text=True,
    )
    assert json.loads(result.stdout) == []


def test_struct_scan_reexports_migration_contract():
    struct_scan = importlib.import_module("struct_scan")
    assert struct_scan.VERSION == VERSION
    assert struct_scan.SCHEMA_SQL == SCHEMA_SQL
    assert struct_scan.MIGRATION_HANDLERS is MIGRATION_HANDLERS
    assert struct_scan._migrate_v6_to_v7 is _migrate_v6_to_v7
    assert struct_scan._migrate_v10_to_v11 is _migrate_v10_to_v11
    assert struct_scan._resolve_migration_path is _resolve_migration_path


def test_ladder_reentry_idempotent(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    for _ in range(3):
        _migrate_v6_to_v7(db)

    sv_count = db.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0]
    assert sv_count == 1
    fts_count = db.execute("SELECT COUNT(*) FROM summary_fts").fetchone()[0]
    assert fts_count == 1
    log_rows = db.execute("SELECT from_version, to_version FROM migration_log").fetchall()
    assert log_rows == [("6.0.0", "7.0.0")]

    sym_cols = {c[1] for c in db.execute("PRAGMA table_info(symbols)").fetchall()}
    assert "summary" not in sym_cols

    file_cols = {c[1] for c in db.execute("PRAGMA table_info(files)").fetchall()}
    assert "kind_hint" in file_cols
    assert "actual_kind" in file_cols

    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "symbols_fts" not in tables
    db.close()


def test_fts_trigger_not_fired_on_insert_or_ignore(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    _migrate_v6_to_v7(db)

    before = db.execute("SELECT COUNT(*) FROM summary_fts").fetchone()[0]
    duplicate_payload = json.dumps({"short": "Replaced", "full": None}, ensure_ascii=False)
    db.execute(
        "INSERT OR IGNORE INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("symbol", "src/foo.py::alpha", 1, duplicate_payload, "ok", "2026-06-29"),
    )
    db.commit()

    after = db.execute("SELECT COUNT(*) FROM summary_fts").fetchone()[0]
    assert after == before

    existing_short = db.execute(
        "SELECT json_extract(summary, '$.short') FROM summary_versions "
        "WHERE node_kind='symbol' AND node_ref='src/foo.py::alpha'"
    ).fetchone()[0]
    assert existing_short == "Computes alpha"
    db.close()


def test_recovery_from_residual_db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    db.executescript(SCHEMA_SQL)
    seed_payload = json.dumps({"short": "Computes alpha", "full": None}, ensure_ascii=False)
    db.execute(
        "INSERT OR IGNORE INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("symbol", "src/foo.py::alpha", 1, seed_payload, "ok", "2026-06-29"),
    )
    db.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
    db.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
    db.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
    db.execute("DROP TABLE IF EXISTS symbols_fts")
    db.commit()

    sv_before = db.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0]
    assert sv_before == 1

    _migrate_v6_to_v7(db)

    sv_after = db.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0]
    assert sv_after == 1

    file_cols = {c[1] for c in db.execute("PRAGMA table_info(files)").fetchall()}
    assert "kind_hint" in file_cols
    assert "actual_kind" in file_cols

    sym_cols = {c[1] for c in db.execute("PRAGMA table_info(symbols)").fetchall()}
    assert "summary" not in sym_cols

    log_rows = db.execute("SELECT from_version, to_version FROM migration_log").fetchall()
    assert log_rows == [("6.0.0", "7.0.0")]
    db.close()


def test_v7_to_v8_creates_occurrences_and_invalidates_hashes(tmp_path):
    db = _make_v7_db(tmp_path / "logic_index.db")
    _migrate_v7_to_v8(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "symbol_occurrences" in tables
    assert db.execute("SELECT struct_hash FROM files").fetchone()[0] == ""
    logs = db.execute(
        "SELECT from_version, to_version FROM migration_log ORDER BY id"
    ).fetchall()
    assert logs[-1] == ("7.0.0", "8.0.0")
    db.close()


def test_v7_to_v8_reentry_is_idempotent(tmp_path):
    db = _make_v7_db(tmp_path / "logic_index.db")
    for _ in range(3):
        _migrate_v7_to_v8(db)
    count = db.execute(
        "SELECT COUNT(*) FROM migration_log WHERE from_version='7.0.0' AND to_version='8.0.0'"
    ).fetchone()[0]
    assert count == 1
    db.close()


def test_v7_to_v8_rolls_back_on_failure(tmp_path):
    real_db = _make_v7_db(tmp_path / "logic_index.db")
    wrapper = _FailOnMatch(real_db, "UPDATE files SET struct_hash")
    with pytest.raises(sqlite3.IntegrityError):
        _migrate_v7_to_v8(wrapper)
    tables = {r[0] for r in real_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "symbol_occurrences" not in tables
    assert real_db.execute("SELECT struct_hash FROM files").fetchone()[0] == "h1"
    real_db.close()


def _make_v8_db(path):
    db = _make_v7_db(path)
    _migrate_v7_to_v8(db)
    db.execute("UPDATE meta SET value='8.0.0' WHERE key='version'")
    db.execute("UPDATE files SET struct_hash='h1'")
    db.commit()
    return db


def test_v8_to_v9_builds_current_projection_and_removes_old_fts(tmp_path):
    db = _make_v8_db(tmp_path / "logic_index.db")
    db.execute(
        "INSERT INTO summary_versions "
        "(node_kind,node_ref,version,summary,status,created_at) "
        "VALUES ('symbol','src/foo.py::alpha',2,?, 'ok','2026-01-01')",
        (json.dumps({"short": "current alpha", "full": None}),),
    )
    db.commit()
    _migrate_v8_to_v9(db)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "retrieval_documents" in tables
    assert "retrieval_fts" in tables
    assert "summary_fts" not in tables
    row = db.execute(
        "SELECT node_kind,node_ref,summary_short,source_version "
        "FROM retrieval_documents WHERE node_kind='symbol'"
    ).fetchone()
    assert row == ("symbol", "src/foo.py::alpha", "current alpha", 2)
    db.close()


def test_v8_to_v9_reentry_is_idempotent(tmp_path):
    db = _make_v8_db(tmp_path / "logic_index.db")
    for _ in range(3):
        _migrate_v8_to_v9(db)
    assert db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='8.0.0' AND to_version='9.0.0'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM retrieval_documents WHERE node_kind='symbol'"
    ).fetchone()[0] == 1
    db.close()


def test_v8_to_v9_rolls_back_on_failure(tmp_path):
    real_db = _make_v8_db(tmp_path / "logic_index.db")
    wrapper = _FailOnMatch(real_db, "DROP TABLE IF EXISTS summary_fts")
    with pytest.raises(sqlite3.IntegrityError):
        _migrate_v8_to_v9(wrapper)
    tables = {r[0] for r in real_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "summary_fts" in tables
    assert "retrieval_documents" not in tables
    assert real_db.execute(
        "SELECT COUNT(*) FROM summary_versions"
    ).fetchone()[0] == 1
    real_db.close()


def _make_v9_db(path):
    db = _make_v8_db(path)
    _migrate_v8_to_v9(db)
    db.execute("UPDATE meta SET value='9.0.0' WHERE key='version'")
    db.execute("""
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            caller TEXT NOT NULL,
            callee TEXT NOT NULL,
            callee_file TEXT,
            callee_qualified TEXT,
            line INTEGER,
            provenance TEXT,
            synthesized_from TEXT,
            via TEXT
        )
    """)
    db.commit()
    return db


def test_v9_to_v10_deduplicates_inferred_edges_and_rebuilds_hashes(tmp_path):
    db = _make_v9_db(tmp_path / "logic_index.db")
    db.execute(
        "INSERT INTO files (path, struct_hash) VALUES ('src/bar.py', 'h2')"
    )
    edge = (
        "src/foo.py", "alpha", "beta", "src/bar.py", "src/bar.py::beta",
        9, "inferred", "src/foo.py", "interface-impl",
    )
    db.execute(
        "INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,"
        "line,provenance,synthesized_from,via) VALUES (?,?,?,?,?,?,?,?,?)",
        edge,
    )
    db.execute(
        "INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,"
        "line,provenance,synthesized_from,via) VALUES (?,?,?,?,?,?,?,?,?)",
        edge[:5] + (3,) + edge[6:],
    )
    before_hash = "v9-hash-including-source-version"
    db.execute(
        "UPDATE retrieval_documents SET content_hash = ? "
        "WHERE node_kind='symbol' AND node_ref='src/foo.py::alpha'",
        (before_hash,),
    )
    db.commit()

    _migrate_v9_to_v10(db)

    rows = db.execute(
        "SELECT line FROM edges WHERE provenance='inferred'"
    ).fetchall()
    assert rows == [(3,)]
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_edges_inferred_identity'"
    ).fetchone() == (1,)
    after = db.execute(
        "SELECT node_kind,node_ref,language,symbol_type,file_path,name,name_tokens,"
        "signature,summary_short,summary_full,content_hash,source_version "
        "FROM retrieval_documents WHERE node_kind='symbol' "
        "AND node_ref='src/foo.py::alpha'"
    ).fetchone()
    hash_payload = {
        "node_kind": after[0],
        "node_ref": after[1],
        "language": after[2],
        "symbol_type": after[3],
        "file_path": after[4],
        "name": after[5],
        "name_tokens": after[6],
        "signature": after[7],
        "summary_short": after[8],
        "summary_full": after[9],
    }
    expected_hash = hashlib.sha256(
        json.dumps(
            hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert before_hash != after[10]
    assert after[10] == expected_hash
    assert after[11] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='9.0.0' AND to_version='10.0.0'"
    ).fetchone()[0] == 1
    db.close()


def test_v9_to_v10_reentry_is_idempotent(tmp_path):
    db = _make_v9_db(tmp_path / "logic_index.db")
    for _ in range(3):
        _migrate_v9_to_v10(db)
    assert db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='9.0.0' AND to_version='10.0.0'"
    ).fetchone()[0] == 1
    db.close()


def test_v9_to_v10_rolls_back_on_failure(tmp_path):
    real_db = _make_v9_db(tmp_path / "logic_index.db")
    real_db.execute(
        "INSERT INTO files (path, struct_hash) VALUES ('src/bar.py', 'h2')"
    )
    edge = (
        "src/foo.py", "alpha", "beta", "src/bar.py", "src/bar.py::beta",
        2, "inferred", "src/foo.py", "interface-impl",
    )
    for _ in range(2):
        real_db.execute(
            "INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,"
            "line,provenance,synthesized_from,via) VALUES (?,?,?,?,?,?,?,?,?)",
            edge,
        )
    real_db.commit()
    wrapper = _FailOnMatch(real_db, "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_inferred_identity")
    with pytest.raises(sqlite3.IntegrityError):
        _migrate_v9_to_v10(wrapper)
    assert real_db.execute(
        "SELECT COUNT(*) FROM edges WHERE provenance='inferred'"
    ).fetchone()[0] == 2
    assert real_db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_edges_inferred_identity'"
    ).fetchone() is None
    assert real_db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='9.0.0' AND to_version='10.0.0'"
    ).fetchone()[0] == 0
    real_db.close()


def _make_v10_db(path):
    db = _make_v9_db(path)
    _migrate_v9_to_v10(db)
    db.execute("UPDATE meta SET value='10.0.0' WHERE key='version'")
    db.commit()
    return db


def test_v10_to_v11_adds_empty_parser_identities_and_preserves_facts(tmp_path):
    db = _make_v10_db(tmp_path / "logic_index.db")
    before_hash = db.execute(
        "SELECT struct_hash FROM files WHERE path='src/foo.py'"
    ).fetchone()
    before_symbols = db.execute(
        "SELECT file_path,name,hash FROM symbols ORDER BY file_path,name"
    ).fetchall()
    _migrate_v10_to_v11(db)
    columns = {column[1] for column in db.execute("PRAGMA table_info(files)")}
    assert {
        "parser_contract_version", "parser_backend", "parser_environment"
    }.issubset(columns)
    row = db.execute(
        "SELECT parser_contract_version,parser_backend,parser_environment "
        "FROM files WHERE path='src/foo.py'"
    ).fetchone()
    assert row == ("", "", "{}")
    assert db.execute(
        "SELECT struct_hash FROM files WHERE path='src/foo.py'"
    ).fetchone() == before_hash
    assert db.execute(
        "SELECT file_path,name,hash FROM symbols ORDER BY file_path,name"
    ).fetchall() == before_symbols
    db.close()


def test_v10_to_v11_reentry_is_idempotent(tmp_path):
    db = _make_v10_db(tmp_path / "logic_index.db")
    for _ in range(3):
        _migrate_v10_to_v11(db)
    assert db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='10.0.0' AND to_version='11.0.0'"
    ).fetchone()[0] == 1
    db.close()


def test_v10_to_v11_rolls_back_on_failure(tmp_path):
    real_db = _make_v10_db(tmp_path / "logic_index.db")
    wrapper = _FailOnMatch(real_db, "parser_backend")
    with pytest.raises(sqlite3.IntegrityError):
        _migrate_v10_to_v11(wrapper)
    columns = {column[1] for column in real_db.execute("PRAGMA table_info(files)")}
    assert "parser_contract_version" not in columns
    assert "parser_backend" not in columns
    assert "parser_environment" not in columns
    assert real_db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='10.0.0' AND to_version='11.0.0'"
    ).fetchone()[0] == 0
    real_db.close()


def _make_v11_db(path):
    db = _make_v10_db(path)
    _migrate_v10_to_v11(db)
    db.execute("UPDATE meta SET value='11.0.0' WHERE key='version'")
    db.commit()
    return db


def test_v11_to_v12_adds_call_form_and_import_bindings_and_preserves_facts(tmp_path):
    db = _make_v11_db(tmp_path / "logic_index.db")
    before_symbols = db.execute(
        "SELECT file_path,name,hash FROM symbols ORDER BY file_path,name"
    ).fetchall()
    before_edges = db.execute(
        "SELECT source_file,caller,callee,provenance FROM edges "
        "ORDER BY source_file,caller,callee"
    ).fetchall()
    _migrate_v11_to_v12(db)
    edge_columns = {column[1] for column in db.execute("PRAGMA table_info(edges)")}
    assert "call_form" in edge_columns
    file_columns = {column[1] for column in db.execute("PRAGMA table_info(files)")}
    assert "import_bindings" in file_columns
    assert db.execute(
        "SELECT import_bindings FROM files WHERE path='src/foo.py'"
    ).fetchone() == ("[]",)
    forms = {row[0] for row in db.execute("SELECT call_form FROM edges")}
    assert forms <= {"name"}
    assert db.execute(
        "SELECT file_path,name,hash FROM symbols ORDER BY file_path,name"
    ).fetchall() == before_symbols
    assert db.execute(
        "SELECT source_file,caller,callee,provenance FROM edges "
        "ORDER BY source_file,caller,callee"
    ).fetchall() == before_edges
    db.close()


def test_v11_to_v12_reentry_is_idempotent(tmp_path):
    db = _make_v11_db(tmp_path / "logic_index.db")
    for _ in range(3):
        _migrate_v11_to_v12(db)
    assert db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='11.0.0' AND to_version='12.0.0'"
    ).fetchone()[0] == 1
    db.close()


def test_v11_to_v12_rolls_back_on_failure(tmp_path):
    real_db = _make_v11_db(tmp_path / "logic_index.db")
    wrapper = _FailOnMatch(real_db, "import_bindings")
    with pytest.raises(sqlite3.IntegrityError):
        _migrate_v11_to_v12(wrapper)
    edge_columns = {column[1] for column in real_db.execute("PRAGMA table_info(edges)")}
    assert "call_form" not in edge_columns
    file_columns = {column[1] for column in real_db.execute("PRAGMA table_info(files)")}
    assert "import_bindings" not in file_columns
    assert real_db.execute(
        "SELECT COUNT(*) FROM migration_log "
        "WHERE from_version='11.0.0' AND to_version='12.0.0'"
    ).fetchone()[0] == 0
    real_db.close()


def test_schema_sql_is_idempotent(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    try:
        db.executescript(SCHEMA_SQL)
        schema_before = sorted(
            db.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        db.executescript(SCHEMA_SQL)
        schema_after = sorted(
            db.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        assert schema_before == schema_after
    finally:
        db.close()


def test_init_db_creates_backup_on_version_mismatch(tmp_path):
    StructScanner = importlib.import_module("struct_scan").StructScanner

    db_dir = tmp_path / ".claude"
    db_dir.mkdir()
    db_path = db_dir / "logic_index.db"
    db = _make_v7_db(db_path)
    db.close()

    scanner = StructScanner(str(tmp_path))
    try:
        bak_path = db_dir / "logic_index.db.bak"
        assert bak_path.exists()

        bak_db = sqlite3.connect(str(bak_path))
        try:
            bak_version = bak_db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            assert bak_version[0] == "7.0.0"
            bak_alpha = bak_db.execute(
                "SELECT json_extract(summary, '$.short') FROM summary_versions "
                "WHERE node_ref='src/foo.py::alpha'"
            ).fetchone()
            assert bak_alpha == ("Computes alpha",)
        finally:
            bak_db.close()

        post_version = scanner.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        assert post_version[0] == VERSION
    finally:
        scanner.db.close()


def test_init_db_backup_includes_committed_wal_rows(tmp_path):
    StructScanner = importlib.import_module("struct_scan").StructScanner

    db_dir = tmp_path / ".claude"
    db_dir.mkdir()
    db_path = db_dir / "logic_index.db"
    db = _make_v7_db(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "INSERT INTO files (path, struct_hash, language, layer, imports) "
        "VALUES ('wal.py', 'wal-hash', 'PythonParser', 'Core', '[]')"
    )
    db.commit()
    db.close()

    scanner = StructScanner(str(tmp_path))
    scanner.db.close()
    backup = sqlite3.connect(str(db_path) + ".bak")
    try:
        assert backup.execute("SELECT path FROM files WHERE path='wal.py'").fetchone() == ("wal.py",)
    finally:
        backup.close()


def test_init_db_rejects_existing_database_without_version(tmp_path):
    StructScanner = importlib.import_module("struct_scan").StructScanner

    db_dir = tmp_path / ".claude"
    db_dir.mkdir()
    db_path = db_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL)")
    db.commit()
    db.close()

    with pytest.raises(RuntimeError, match="no schema version"):
        StructScanner(str(tmp_path))
    db = sqlite3.connect(str(db_path))
    try:
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"files"}
    finally:
        db.close()
