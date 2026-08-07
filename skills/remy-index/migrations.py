"""SQLite schema initialization and migration ladder."""

import json
import os
import sqlite3
import sys
from datetime import datetime

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

from constants import DB_BUSY_TIMEOUT_MS
from retrieval_projection import (
    create_projection_schema,
    rebuild_projection,
)
from schema import SCHEMA_SQL, VERSION
from symbol_names import tokenize_symbol


JSON_CACHE_FILE = os.path.join(".claude", "logic_index.json")


def _migrate_v6_to_v7(db):
    """Apply v6 -> v7 schema migration as a single atomic transaction.

    Each migration handler creates the objects it owns so it can run inside its
    own transaction before _init_db applies the complete current schema. This
    handler is therefore safe to invoke directly from migration tests.

    All SQL statements are issued through single db.execute() calls.
    executescript() is prohibited because CPython's sqlite3 module performs
    an implicit COMMIT before each executescript() invocation, which would
    silently destroy the BEGIN IMMEDIATE transaction and break atomic rollback.

    Idempotent re-entry: every CREATE uses IF NOT EXISTS, the data migration
    uses INSERT OR IGNORE, the ALTER operations check PRAGMA table_info first,
    and the migration_log INSERT is guarded by a SELECT EXISTS lookup.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS summary_versions (
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
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS node_change_counters (
                node_kind TEXT NOT NULL,
                node_ref TEXT NOT NULL,
                child_change_count INTEGER NOT NULL DEFAULT 0,
                leaf_descendant_count INTEGER NOT NULL DEFAULT 0,
                last_force_recompute_at TEXT,
                PRIMARY KEY (node_kind, node_ref)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS judge_cache (
                payload_hash TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS migration_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_version TEXT NOT NULL,
                to_version TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_sv_lookup ON summary_versions(node_kind, node_ref, version DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sv_status ON summary_versions(status, node_kind)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_jc_created ON judge_cache(created_at)")
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS summary_fts USING fts5(
                node_kind UNINDEXED,
                node_ref UNINDEXED,
                short,
                full,
                tokenize='unicode61'
            )
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS summary_fts_ai AFTER INSERT ON summary_versions
            WHEN NEW.status NOT IN ('pending', 'corrupt') BEGIN
                INSERT INTO summary_fts(rowid, node_kind, node_ref, short, full)
                VALUES (NEW.id, NEW.node_kind, NEW.node_ref,
                        COALESCE(json_extract(NEW.summary, '$.short'), ''),
                        COALESCE(json_extract(NEW.summary, '$.full'), ''));
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS summary_fts_ad AFTER DELETE ON summary_versions BEGIN
                DELETE FROM summary_fts WHERE rowid = OLD.id;
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS summary_fts_au AFTER UPDATE ON summary_versions BEGIN
                DELETE FROM summary_fts WHERE rowid = OLD.id;
                INSERT INTO summary_fts(rowid, node_kind, node_ref, short, full)
                SELECT NEW.id, NEW.node_kind, NEW.node_ref,
                       COALESCE(json_extract(NEW.summary, '$.short'), ''),
                       COALESCE(json_extract(NEW.summary, '$.full'), '')
                WHERE NEW.status NOT IN ('pending', 'corrupt');
            END
        """)

        now_iso = datetime.now().isoformat(timespec='seconds')
        legacy_cols = {c[1] for c in db.execute("PRAGMA table_info(symbols)").fetchall()}
        if 'summary' in legacy_cols:
            rows = db.execute(
                "SELECT file_path, name, summary FROM symbols WHERE summary IS NOT NULL"
            ).fetchall()
            for fp, name, summary_text in rows:
                node_ref = f"{fp}::{name}"
                payload = json.dumps({"short": summary_text, "full": None}, ensure_ascii=False)
                db.execute(
                    "INSERT OR IGNORE INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) VALUES (?,?,?,?,?,?)",
                    ('symbol', node_ref, 1, payload, 'ok', now_iso)
                )

        db.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
        db.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
        db.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
        db.execute("DROP TABLE IF EXISTS symbols_fts")

        file_cols = {c[1] for c in db.execute("PRAGMA table_info(files)").fetchall()}
        if 'kind_hint' not in file_cols:
            db.execute("ALTER TABLE files ADD COLUMN kind_hint TEXT")
        if 'actual_kind' not in file_cols:
            db.execute("ALTER TABLE files ADD COLUMN actual_kind TEXT")

        sym_cols = {c[1] for c in db.execute("PRAGMA table_info(symbols)").fetchall()}
        if 'summary' in sym_cols:
            db.execute("ALTER TABLE symbols DROP COLUMN summary")

        already = db.execute(
            "SELECT 1 FROM migration_log WHERE from_version=? AND to_version=?",
            ('6.0.0', '7.0.0')
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO migration_log (from_version, to_version, applied_at) VALUES (?,?,?)",
                ('6.0.0', '7.0.0', now_iso)
            )

        db.commit()
    except Exception:
        db.rollback()
        raise


def _migrate_v7_to_v8(db):
    """Add auditable same-name occurrences and invalidate file scan hashes."""
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS symbol_occurrences (
                file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                name TEXT NOT NULL,
                occurrence_index INTEGER NOT NULL,
                type TEXT NOT NULL,
                args TEXT,
                lineno INTEGER,
                end_lineno INTEGER,
                hash TEXT NOT NULL,
                is_canonical INTEGER NOT NULL CHECK (is_canonical IN (0, 1)),
                conflict_kind TEXT NOT NULL CHECK (
                    conflict_kind IN ('type_variant', 'signature_variant', 'duplicate_definition')
                ),
                selection_reason TEXT NOT NULL,
                PRIMARY KEY (file_path, name, occurrence_index)
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_occurrences_file_name "
            "ON symbol_occurrences(file_path, name)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_occurrences_one_canonical "
            "ON symbol_occurrences(file_path, name) WHERE is_canonical = 1"
        )
        db.execute("UPDATE files SET struct_hash = ''")
        already = db.execute(
            "SELECT 1 FROM migration_log WHERE from_version=? AND to_version=?",
            ('7.0.0', '8.0.0')
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO migration_log (from_version, to_version, applied_at) VALUES (?,?,?)",
                ('7.0.0', '8.0.0', datetime.now().isoformat(timespec='seconds'))
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _migrate_v8_to_v9(db):
    """Create the current retrieval projection and retire historical-summary FTS."""
    db.execute("BEGIN IMMEDIATE")
    try:
        create_projection_schema(db)
        rebuild_projection(db)
        db.execute("DROP TRIGGER IF EXISTS summary_fts_ai")
        db.execute("DROP TRIGGER IF EXISTS summary_fts_ad")
        db.execute("DROP TRIGGER IF EXISTS summary_fts_au")
        db.execute("DROP TABLE IF EXISTS summary_fts")
        already = db.execute(
            "SELECT 1 FROM migration_log WHERE from_version=? AND to_version=?",
            ('8.0.0', '9.0.0')
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO migration_log (from_version, to_version, applied_at) VALUES (?,?,?)",
                ('8.0.0', '9.0.0', datetime.now().isoformat(timespec='seconds'))
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _migrate_v9_to_v10(db):
    """Rebuild content hashes and enforce one inferred edge per identity."""
    db.execute("BEGIN IMMEDIATE")
    try:
        has_edges = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edges'"
        ).fetchone() is not None
        if has_edges:
            duplicate_groups = db.execute(
                "SELECT source_file, caller, callee_qualified, via "
                "FROM edges WHERE provenance = 'inferred' "
                "AND callee_qualified IS NOT NULL AND via IS NOT NULL "
                "GROUP BY source_file, caller, callee_qualified, via "
                "HAVING COUNT(*) > 1 "
                "ORDER BY source_file, caller, callee_qualified, via"
            ).fetchall()
            for identity in duplicate_groups:
                rows = db.execute(
                    "SELECT id FROM edges WHERE provenance = 'inferred' "
                    "AND source_file = ? AND caller = ? AND callee_qualified = ? "
                    "AND via = ? "
                    "ORDER BY COALESCE(line, 0), COALESCE(synthesized_from, ''), id",
                    identity,
                ).fetchall()
                duplicate_ids = [row[0] for row in rows[1:]]
                if duplicate_ids:
                    placeholders = ','.join(['?'] * len(duplicate_ids))
                    db.execute(
                        f"DELETE FROM edges WHERE id IN ({placeholders})",
                        duplicate_ids,
                    )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_inferred_identity "
                "ON edges(source_file, caller, callee_qualified, via) "
                "WHERE provenance = 'inferred' "
                "AND callee_qualified IS NOT NULL AND via IS NOT NULL"
            )
        rebuild_projection(db)
        already = db.execute(
            "SELECT 1 FROM migration_log WHERE from_version=? AND to_version=?",
            ('9.0.0', '10.0.0')
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO migration_log (from_version, to_version, applied_at) "
                "VALUES (?,?,?)",
                ('9.0.0', '10.0.0', datetime.now().isoformat(timespec='seconds'))
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _migrate_v10_to_v11(db):
    """Add per-file parser cache identities without invalidating source hashes."""
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {column[1] for column in db.execute("PRAGMA table_info(files)")}
        additions = (
            ("parser_contract_version", "TEXT NOT NULL DEFAULT ''"),
            ("parser_backend", "TEXT NOT NULL DEFAULT ''"),
            ("parser_environment", "TEXT NOT NULL DEFAULT '{}'"),
        )
        for name, declaration in additions:
            if name not in columns:
                db.execute(f"ALTER TABLE files ADD COLUMN {name} {declaration}")

        already = db.execute(
            "SELECT 1 FROM migration_log WHERE from_version=? AND to_version=?",
            ("10.0.0", "11.0.0"),
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO migration_log (from_version, to_version, applied_at) "
                "VALUES (?,?,?)",
                ("10.0.0", "11.0.0", datetime.now().isoformat(timespec="seconds")),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _migrate_v11_to_v12(db):
    """Add edge call-form and per-file unresolved import bindings.

    Tables absent from pre-v12 ladders (edges did not exist at v6) are
    skipped: initialize_database applies SCHEMA_SQL afterwards, which
    creates them with the new columns already in place.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        edge_columns = {column[1] for column in db.execute("PRAGMA table_info(edges)")}
        if edge_columns and "call_form" not in edge_columns:
            db.execute("ALTER TABLE edges ADD COLUMN call_form TEXT NOT NULL DEFAULT 'name'")
        file_columns = {column[1] for column in db.execute("PRAGMA table_info(files)")}
        if file_columns and "import_bindings" not in file_columns:
            db.execute("ALTER TABLE files ADD COLUMN import_bindings TEXT NOT NULL DEFAULT '[]'")

        already = db.execute(
            "SELECT 1 FROM migration_log WHERE from_version=? AND to_version=?",
            ("11.0.0", "12.0.0"),
        ).fetchone()
        if not already:
            db.execute(
                "INSERT INTO migration_log (from_version, to_version, applied_at) "
                "VALUES (?,?,?)",
                ("11.0.0", "12.0.0", datetime.now().isoformat(timespec="seconds")),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


MIGRATION_HANDLERS = {
    ('6.0.0', '7.0.0'): _migrate_v6_to_v7,
    ('7.0.0', '8.0.0'): _migrate_v7_to_v8,
    ('8.0.0', '9.0.0'): _migrate_v8_to_v9,
    ('9.0.0', '10.0.0'): _migrate_v9_to_v10,
    ('10.0.0', '11.0.0'): _migrate_v10_to_v11,
    ('11.0.0', '12.0.0'): _migrate_v11_to_v12,
}

def _resolve_migration_path(old_version, new_version):
    if old_version == new_version:
        return []
    direct = MIGRATION_HANDLERS.get((old_version, new_version))
    if direct:
        return [(old_version, new_version, direct)]
    chain = []
    current = old_version
    visited = {current}
    while current != new_version:
        next_step = None
        for (frm, to), handler in MIGRATION_HANDLERS.items():
            if frm == current and to not in visited:
                next_step = (frm, to, handler)
                break
        if not next_step:
            return None
        chain.append(next_step)
        current = next_step[1]
        visited.add(current)
    return chain


def initialize_database(root_dir, db_path):
    db_existed = os.path.exists(db_path)
    needs_migration = (
        not db_existed
        and os.path.exists(os.path.join(root_dir, JSON_CACHE_FILE))
    )
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
    else:
        if 'meta' not in existing_tables:
            db.close()
            raise RuntimeError(
                f"Existing logic_index.db at {db_path} has no schema version. "
                "The database is preserved unchanged."
            )
        version_row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if not version_row:
            db.close()
            raise RuntimeError(
                f"Existing logic_index.db at {db_path} has no schema version. "
                "The database is preserved unchanged."
            )
        if version_row[0] != VERSION:
            chain = _resolve_migration_path(version_row[0], VERSION)
            if chain is None:
                db.close()
                raise RuntimeError(
                    f"Migration path v{version_row[0]} -> v{VERSION} missing handler. "
                    f"The logic_index.db at {db_path} is preserved unchanged. "
                    f"Available handlers: {sorted(MIGRATION_HANDLERS.keys())}"
                )
            backup_path = db_path + '.bak'
            backup_db = None
            try:
                backup_db = sqlite3.connect(backup_path)
                db.backup(backup_db)
            except (OSError, sqlite3.Error) as backup_err:
                db.close()
                raise RuntimeError(
                    f"Failed to back up logic_index.db before migration: {backup_err}. "
                    f"Migration aborted to avoid data loss."
                ) from backup_err
            finally:
                if backup_db is not None:
                    backup_db.close()
            for _, _, handler in chain:
                handler(db)
            db.executescript(SCHEMA_SQL)
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)",
                (VERSION,)
            )
            db.commit()
        else:
            db.executescript(SCHEMA_SQL)

    if needs_migration:
        migrate_json(root_dir, db)

    return db

def migrate_json(root_dir, db):
    json_path = os.path.join(root_dir, JSON_CACHE_FILE)
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return

    try:
        now_iso = datetime.now().isoformat(timespec='seconds')
        for path, file_data in data.items():
            if path == "_meta":
                continue
            db.execute(
                "INSERT OR IGNORE INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,?,?,?)",
                (path, file_data.get("struct_hash", ""),
                 file_data.get("language", ""),
                 file_data.get("layer", "Core"),
                 json.dumps(file_data.get("imports", [])))
            )
            for sym in file_data.get("symbols", []):
                bases_json = json.dumps(sym["bases"]) if sym.get("bases") else None
                short = sym["name"].split(".")[-1] if "." in sym["name"] else sym["name"]
                tokens = tokenize_symbol(sym["name"])
                db.execute(
                    "INSERT OR IGNORE INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, bases, name_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (path, sym["name"], short, sym.get("type", "function"),
                     sym.get("args"), sym.get("lineno"), sym.get("end_lineno"),
                     sym.get("hash"), bases_json, tokens)
                )
                summary_text = sym.get("summary")
                if summary_text:
                    node_ref = f"{path}::{sym['name']}"
                    payload = json.dumps({"short": summary_text, "full": None}, ensure_ascii=False)
                    db.execute(
                        "INSERT OR IGNORE INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) VALUES (?,?,?,?,?,?)",
                        ('symbol', node_ref, 1, payload, 'ok', now_iso)
                    )
            for call in file_data.get("calls", []):
                db.execute(
                    "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified, line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                    (path, call["caller"], call["callee"],
                     call.get("callee_file"), call.get("callee_qualified"),
                     call.get("line"), call.get("provenance"),
                     call.get("synthesized_from"), call.get("via"))
                )
        rebuild_projection(db)
        db.commit()

        keep = remy_config.load_config(root_dir, strict=True).get_bool("REMY_MIGRATION_KEEP_JSON")
        if not keep:
            migrated_path = json_path + ".migrated"
            os.rename(json_path, migrated_path)
    except Exception:
        db.rollback()
