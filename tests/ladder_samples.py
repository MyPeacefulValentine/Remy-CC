"""Shared factories for schema-resident-state sample databases.

Extracted verbatim from test_migration_ladder.py (R4.2). Each factory
returns an open sqlite3 connection whose database sits at the named
historical schema version; consumers are the ladder tests and the Rust
rebuild-path tests, which need pre-current databases as inputs.
"""

import importlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
_migrations = importlib.import_module("migrations")

_migrate_v6_to_v7 = _migrations._migrate_v6_to_v7
_migrate_v7_to_v8 = _migrations._migrate_v7_to_v8
_migrate_v8_to_v9 = _migrations._migrate_v8_to_v9
_migrate_v9_to_v10 = _migrations._migrate_v9_to_v10
_migrate_v10_to_v11 = _migrations._migrate_v10_to_v11


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


def _make_v8_db(path):
    db = _make_v7_db(path)
    _migrate_v7_to_v8(db)
    db.execute("UPDATE meta SET value='8.0.0' WHERE key='version'")
    db.execute("UPDATE files SET struct_hash='h1'")
    db.commit()
    return db


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


def _make_v10_db(path):
    db = _make_v9_db(path)
    _migrate_v9_to_v10(db)
    db.execute("UPDATE meta SET value='10.0.0' WHERE key='version'")
    db.commit()
    return db


def _make_v11_db(path):
    db = _make_v10_db(path)
    _migrate_v10_to_v11(db)
    db.execute("UPDATE meta SET value='11.0.0' WHERE key='version'")
    db.commit()
    return db
