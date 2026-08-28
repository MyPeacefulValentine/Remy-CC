"""SQLite schema initialization for the current schema version only.

The Python migration ladder (v6..v12) and the legacy JSON import were
retired in R4.3; `remy-daemon scan` (writer.rs::open_db) is the schema
owner and rebuilds non-current databases. This module refuses every
non-current version and leaves the database unchanged.
"""

import os
import sqlite3

from constants import DB_BUSY_TIMEOUT_MS
from schema import SCHEMA_SQL, VERSION


def initialize_database(root_dir, db_path):
    del root_dir
    db_existed = os.path.exists(db_path)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA foreign_keys=ON")

    existing_tables = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if not db_existed or not existing_tables:
        db.executescript(SCHEMA_SQL)
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
        db.commit()
        return db

    if 'meta' not in existing_tables:
        db.close()
        raise RuntimeError(
            f"Existing logic_index.db at {db_path} has no schema version. "
            "The database is preserved unchanged; run 'remy-daemon scan' "
            "(the schema owner) to rebuild it."
        )
    version_row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not version_row:
        db.close()
        raise RuntimeError(
            f"Existing logic_index.db at {db_path} has no schema version. "
            "The database is preserved unchanged; run 'remy-daemon scan' "
            "(the schema owner) to rebuild it."
        )
    if version_row[0] != VERSION:
        db.close()
        raise RuntimeError(
            f"Existing logic_index.db at {db_path} has schema version "
            f"{version_row[0]}; this module only supports {VERSION}. "
            "The database is preserved unchanged; run 'remy-daemon scan' "
            "(the schema owner) to rebuild it."
        )
    db.executescript(SCHEMA_SQL)
    return db
