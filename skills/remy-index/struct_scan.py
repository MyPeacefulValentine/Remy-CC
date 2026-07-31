#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Structural scanner for logic_index.db (SQLite backend).
Extracts symbols, call graphs, imports, patterns, and line ranges without LLM dependency.
Designed for hook-driven incremental and full scans.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import sqlite3
import fnmatch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers.base import SymbolInfo
from parsers.python_parser import PythonParser
from parsers.c_cpp_parser import CCppParser
from parsers.ts_parser import TSParser
from constants import DB_BUSY_TIMEOUT_MS
from symbol_selection import select_symbols
from index_state import (
    DirtyQueue,
    LockTimeoutError,
    RunStatus,
    ScanResult,
    StageError,
    project_scan_lock,
)

VERSION = "8.0.0"
DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")
JSON_CACHE_FILE = os.path.join(".claude", "logic_index.json")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT,
    kind_hint TEXT,
    actual_kind TEXT
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name TEXT NOT NULL,
    short_name TEXT,
    type TEXT NOT NULL,
    args TEXT,
    lineno INTEGER,
    end_lineno INTEGER,
    hash TEXT,
    bases TEXT,
    name_tokens TEXT NOT NULL DEFAULT '',
    UNIQUE(file_path, name)
);
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
);
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
);
CREATE TABLE IF NOT EXISTS edge_candidates (
    edge_id INTEGER NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    candidate_qualified TEXT NOT NULL,
    score INTEGER DEFAULT 0,
    PRIMARY KEY (edge_id, candidate_qualified)
);
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    pattern_type TEXT NOT NULL,
    signal_name TEXT,
    handler TEXT,
    line INTEGER,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT,
    entry_symbols TEXT NOT NULL,
    file_count INTEGER
);
CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, file_path)
);
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
);
CREATE TABLE IF NOT EXISTS node_change_counters (
    node_kind TEXT NOT NULL,
    node_ref TEXT NOT NULL,
    child_change_count INTEGER NOT NULL DEFAULT 0,
    leaf_descendant_count INTEGER NOT NULL DEFAULT 0,
    last_force_recompute_at TEXT,
    PRIMARY KEY (node_kind, node_ref)
);
CREATE TABLE IF NOT EXISTS judge_cache (
    payload_hash TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_short ON symbols(short_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_occurrences_file_name ON symbol_occurrences(file_path, name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_occurrences_one_canonical
ON symbol_occurrences(file_path, name) WHERE is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_edges_callee_q ON edges(callee_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(source_file, caller);
CREATE INDEX IF NOT EXISTS idx_edges_provenance ON edges(provenance);
CREATE INDEX IF NOT EXISTS idx_edges_source_file ON edges(source_file);
CREATE INDEX IF NOT EXISTS idx_patterns_type_signal ON patterns(pattern_type, signal_name);
CREATE INDEX IF NOT EXISTS idx_patterns_file ON patterns(file_path);
CREATE INDEX IF NOT EXISTS idx_sv_lookup ON summary_versions(node_kind, node_ref, version DESC);
CREATE INDEX IF NOT EXISTS idx_sv_status ON summary_versions(status, node_kind);
CREATE INDEX IF NOT EXISTS idx_jc_created ON judge_cache(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS summary_fts USING fts5(
    node_kind UNINDEXED,
    node_ref UNINDEXED,
    short,
    full,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS summary_fts_ai AFTER INSERT ON summary_versions
WHEN NEW.status NOT IN ('pending', 'corrupt') BEGIN
    INSERT INTO summary_fts(rowid, node_kind, node_ref, short, full)
    VALUES (NEW.id, NEW.node_kind, NEW.node_ref,
            COALESCE(json_extract(NEW.summary, '$.short'), ''),
            COALESCE(json_extract(NEW.summary, '$.full'), ''));
END;

CREATE TRIGGER IF NOT EXISTS summary_fts_ad AFTER DELETE ON summary_versions BEGIN
    DELETE FROM summary_fts WHERE rowid = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS summary_fts_au AFTER UPDATE ON summary_versions BEGIN
    DELETE FROM summary_fts WHERE rowid = OLD.id;
    INSERT INTO summary_fts(rowid, node_kind, node_ref, short, full)
    SELECT NEW.id, NEW.node_kind, NEW.node_ref,
           COALESCE(json_extract(NEW.summary, '$.short'), ''),
           COALESCE(json_extract(NEW.summary, '$.full'), '')
    WHERE NEW.status NOT IN ('pending', 'corrupt');
END;
"""

SUMMARY_STATUS_ENUM = frozenset({'ok', 'pending', 'stale', 'oversized_warn', 'oversized_hard', 'corrupt'})

_STATUS_TRANSITIONS = {
    ('pending', 'llm_success'): 'ok',
    ('pending', 'llm_failure'): 'pending',
    ('pending', 'parse_failure'): 'corrupt',
    ('ok', 'mark_stale'): 'stale',
    ('ok', 'parse_failure'): 'corrupt',
    ('stale', 'rewrite_success'): 'ok',
    ('stale', 'llm_failure'): 'pending',
    ('oversized_warn', 'mark_stale'): 'stale',
    ('oversized_hard', 'mark_stale'): 'stale',
}


def _transition_status(old_status, event):
    if old_status not in SUMMARY_STATUS_ENUM:
        return 'corrupt'
    key = (old_status, event)
    return _STATUS_TRANSITIONS.get(key, old_status)


def _compute_kind_hint(sym_count, intra_edges):
    min_symbols = _env_int("FILE_KIND_MIN_SYMBOLS", 5)
    low_cohesion_threshold = _env_float("FILE_KIND_LOW_COHESION_THRESHOLD", 0.25)
    if sym_count < min_symbols:
        return "trivial"
    density = intra_edges / sym_count if sym_count else 0
    if density < low_cohesion_threshold:
        return "low_cohesion"
    return "cohesive"


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


MIGRATION_HANDLERS = {
    ('6.0.0', '7.0.0'): _migrate_v6_to_v7,
    ('7.0.0', '8.0.0'): _migrate_v7_to_v8,
}


def _resolve_git_head(root_dir, db=None):
    """Locate the git HEAD that covers the indexed sources.

    Returns a ``(head, cwd)`` tuple where ``cwd`` is the directory in
    which the ``git rev-parse`` call succeeded; returns ``(None, None)``
    if no git context can be resolved. ``cwd`` is reusable for follow-up
    git invocations such as ``git status --porcelain``.

    Strategy: (1) run ``git rev-parse HEAD`` with cwd=root_dir — succeeds
    for the standard layout where .git sits at the indexed project root.
    (2) Fall back to inspecting the first row of the ``files`` table to
    infer a subdirectory git repo (e.g. workspaces that host multiple
    sibling repos with no .git at the workspace root).
    """
    candidates = [root_dir]
    if db is not None:
        try:
            row = db.execute("SELECT path FROM files LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        if row:
            inferred = os.path.dirname(os.path.join(root_dir, row[0]))
            candidates.append(inferred)
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        try:
            head = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], text=True,
                stderr=subprocess.DEVNULL, cwd=candidate
            ).strip()
            return head, candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None, None


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


def tokenize_symbol(name):
    """Split snake_case, camelCase, and namespace separators into space-separated tokens."""
    s = name.replace("_", " ").replace("::", " ")
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip()


def _env_int(name, default):
    try:
        value = os.environ.get(name)
        return int(value if value is not None else default)
    except (ValueError, TypeError):
        return default


def _env_float(name, default):
    try:
        value = os.environ.get(name)
        return float(value if value is not None else default)
    except (ValueError, TypeError):
        return default


class StructScanner:
    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)
        self.exclusions = []
        self.layers = []
        self._load_config()

        self.filter_small = str(os.environ.get("LOGIC_INDEX_FILTER_SMALL", "false")).lower() == "true"
        self.parsers = [PythonParser(), CCppParser(), TSParser()]
        self._extension_map = {}
        for parser in self.parsers:
            for ext in parser.get_extensions():
                self._extension_map[ext] = parser

        db_rel = os.environ.get("LOGIC_INDEX_DB_PATH", DB_FILE_DEFAULT)
        self.db_path = os.path.join(self.root_dir, db_rel)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = self._init_db()

    def _init_db(self):
        db_existed = os.path.exists(self.db_path)
        needs_migration = (
            not db_existed
            and os.path.exists(os.path.join(self.root_dir, JSON_CACHE_FILE))
        )
        db = sqlite3.connect(self.db_path)
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
                    f"Existing logic_index.db at {self.db_path} has no schema version. "
                    "The database is preserved unchanged."
                )
            version_row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
            if not version_row:
                db.close()
                raise RuntimeError(
                    f"Existing logic_index.db at {self.db_path} has no schema version. "
                    "The database is preserved unchanged."
                )
            if version_row[0] != VERSION:
                chain = _resolve_migration_path(version_row[0], VERSION)
                if chain is None:
                    db.close()
                    raise RuntimeError(
                        f"Migration path v{version_row[0]} -> v{VERSION} missing handler. "
                        f"The logic_index.db at {self.db_path} is preserved unchanged. "
                        f"Available handlers: {sorted(MIGRATION_HANDLERS.keys())}"
                    )
                backup_path = self.db_path + '.bak'
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
                for _from_version, _to_version, handler in chain:
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
            self._migrate_json(db)

        return db

    def _migrate_json(self, db):
        json_path = os.path.join(self.root_dir, JSON_CACHE_FILE)
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
            db.commit()

            keep = str(os.environ.get("MIGRATION_KEEP_JSON", "false")).lower() == "true"
            if not keep:
                migrated_path = json_path + ".migrated"
                os.rename(json_path, migrated_path)
        except Exception:
            db.rollback()

    def _get_parser_for_file(self, filename):
        for ext, parser in self._extension_map.items():
            if filename.endswith(ext):
                return parser
        return None

    def _load_config(self):
        config_path = os.path.join(self.root_dir, CONFIG_FILE)
        if not os.path.exists(config_path):
            try:
                template_path = os.path.join(os.path.dirname(__file__), "default_logic_config.template")
                if os.path.exists(template_path):
                    os.makedirs(os.path.dirname(config_path), exist_ok=True)
                    with open(template_path, "r", encoding="utf-8") as src:
                        content = src.read()
                    with open(config_path, "w", encoding="utf-8") as dst:
                        dst.write(content)
            except Exception:
                pass

        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("!"):
                        self.exclusions.append(line[1:])
                    elif line.startswith("@layer:"):
                        rest = line[len("@layer:"):]
                        if "=" in rest:
                            name, patterns_str = rest.split("=", 1)
                            patterns = [p.strip() for p in patterns_str.split(",") if p.strip()]
                            if name.strip() and patterns:
                                self.layers.append({"name": name.strip(), "patterns": patterns})
        else:
            self.exclusions = [".git/", "__pycache__/", "venv/", "node_modules/", ".claude/", "dist/", "build/"]

    def _is_excluded(self, path):
        rel_path = os.path.relpath(path, self.root_dir).replace(os.sep, "/")
        if rel_path == ".":
            return False
        basename = os.path.basename(rel_path)
        is_dir = os.path.isdir(path)
        for pattern in self.exclusions:
            must_be_dir = pattern.endswith("/")
            clean_pattern = pattern.rstrip("/")
            if must_be_dir and not is_dir:
                continue
            if fnmatch.fnmatch(basename, clean_pattern) or fnmatch.fnmatch(rel_path, clean_pattern):
                return True
        return False

    def _is_path_excluded(self, rel_path):
        rel_path = rel_path.replace("\\", "/")
        parts = rel_path.split("/")
        basename = parts[-1]
        for pattern in self.exclusions:
            must_be_dir = pattern.endswith("/")
            clean_pattern = pattern.rstrip("/")
            if must_be_dir:
                for i, segment in enumerate(parts[:-1]):
                    cumulative = "/".join(parts[:i + 1])
                    if fnmatch.fnmatch(segment, clean_pattern) or fnmatch.fnmatch(cumulative, clean_pattern):
                        return True
            else:
                if fnmatch.fnmatch(basename, clean_pattern) or fnmatch.fnmatch(rel_path, clean_pattern):
                    return True
        return False

    def _match_file_to_layer(self, rel_path):
        segments = rel_path.replace("\\", "/").lower().split("/")
        for layer_def in self.layers:
            for segment in segments:
                for pattern in layer_def["patterns"]:
                    if segment == pattern or segment == pattern + "s":
                        return layer_def["name"]
        return "Core"

    @staticmethod
    def _strip_comments(source, parser):
        try:
            if isinstance(parser, PythonParser):
                return re.sub(r'#[^\n]*', '', source)
            elif isinstance(parser, (CCppParser, TSParser)):
                source = re.sub(r'//[^\n]*', '', source)
                source = re.sub(r'/\*[\s\S]*?\*/', '', source)
                return source
        except Exception:
            pass
        return source

    @staticmethod
    def _calculate_symbol_hash(source_code):
        normalized = "".join(source_code.split())
        return hashlib.md5(normalized.encode('utf-8')).hexdigest()

    @staticmethod
    def _compute_struct_hash(source):
        return hashlib.md5(source.encode('utf-8')).hexdigest()

    def scan_file(self, file_path, parser):
        try:
            rel_path = os.path.relpath(file_path, self.root_dir).replace(os.sep, '/')
        except ValueError:
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
        except Exception:
            return None

        struct_hash = self._compute_struct_hash(source)

        existing = self.db.execute(
            "SELECT struct_hash FROM files WHERE path = ?", (rel_path,)
        ).fetchone()
        if existing and existing[0] == struct_hash:
            return rel_path

        imports = parser.resolve_imports(source, file_path, self.root_dir)
        selection = select_symbols(parser.parse_symbols(source, file_path))
        symbols = selection.canonical_symbols
        call_edges = parser.extract_call_graph(source, file_path)
        pattern_list = parser.extract_patterns(source, file_path)
        layer = self._match_file_to_layer(rel_path)

        self.db.execute(
            "INSERT OR REPLACE INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,?,?,?)",
            (rel_path, struct_hash, parser.__class__.__name__, layer, json.dumps(list(imports.keys())))
        )

        old_hashes = {}
        for row in self.db.execute(
            "SELECT name, hash FROM symbols WHERE file_path = ?", (rel_path,)
        ):
            old_hashes[row[0]] = row[1]

        existing_versions = {}
        for row in self.db.execute(
            "SELECT node_ref, MAX(version) FROM summary_versions WHERE node_kind = 'symbol' AND node_ref LIKE ? GROUP BY node_ref",
            (f"{rel_path}::%",)
        ):
            existing_versions[row[0]] = row[1]

        self.db.execute("DELETE FROM symbols WHERE file_path = ?", (rel_path,))
        self.db.execute("DELETE FROM symbol_occurrences WHERE file_path = ?", (rel_path,))
        self.db.execute("DELETE FROM edges WHERE source_file = ?", (rel_path,))
        self.db.execute("DELETE FROM patterns WHERE file_path = ?", (rel_path,))

        for occurrence in selection.occurrences:
            sym_info = occurrence.symbol
            stripped = self._strip_comments(sym_info.source_segment, parser)
            self.db.execute(
                "INSERT INTO symbol_occurrences "
                "(file_path, name, occurrence_index, type, args, lineno, end_lineno, hash, "
                "is_canonical, conflict_kind, selection_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rel_path, sym_info.name, occurrence.occurrence_index, sym_info.type,
                 sym_info.args, sym_info.lineno, sym_info.end_lineno,
                 self._calculate_symbol_hash(stripped), int(occurrence.is_canonical),
                 occurrence.conflict_kind, occurrence.selection_reason)
            )

        now_iso = datetime.now().isoformat(timespec='seconds')
        for sym_info in symbols:
            stripped = self._strip_comments(sym_info.source_segment, parser)
            symbol_hash = self._calculate_symbol_hash(stripped)
            short_name = sym_info.name.split(".")[-1] if "." in sym_info.name else sym_info.name
            bases_json = json.dumps(sym_info.bases) if sym_info.bases else None
            tokens = tokenize_symbol(sym_info.name)

            self.db.execute(
                "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, bases, name_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rel_path, sym_info.name, short_name, sym_info.type,
                 sym_info.args, sym_info.lineno, sym_info.end_lineno,
                 symbol_hash, bases_json, tokens)
            )

            node_ref = f"{rel_path}::{sym_info.name}"
            hash_unchanged = old_hashes.get(sym_info.name) == symbol_hash
            has_existing_version = node_ref in existing_versions
            if hash_unchanged and has_existing_version:
                continue

            initial_summary = None
            if sym_info.docstring:
                lines = [line.strip() for line in sym_info.docstring.splitlines() if line.strip()]
                if lines:
                    initial_summary = "[Doc] " + " ".join(lines[:3])
            elif self.filter_small and len(sym_info.source_segment.splitlines()) < 3:
                initial_summary = "Small utility function."

            if initial_summary:
                new_version = existing_versions.get(node_ref, 0) + 1
                payload = json.dumps({"short": initial_summary, "full": None}, ensure_ascii=False)
                self.db.execute(
                    "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) VALUES (?,?,?,?,?,?)",
                    ('symbol', node_ref, new_version, payload, 'ok', now_iso)
                )

        seen_edges = {}
        for e in call_edges:
            key = (e.caller, e.callee)
            if key in seen_edges:
                if e.line < seen_edges[key]["line"]:
                    seen_edges[key]["line"] = e.line
                continue
            seen_edges[key] = {
                "caller": e.caller, "callee": e.callee, "line": e.line,
                "provenance": e.provenance, "synthesized_from": e.synthesized_from, "via": e.via,
            }
        for edge in seen_edges.values():
            self.db.execute(
                "INSERT INTO edges (source_file, caller, callee, line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?)",
                (rel_path, edge["caller"], edge["callee"], edge["line"],
                 edge["provenance"], edge["synthesized_from"], edge["via"])
            )

        for pat in pattern_list:
            self.db.execute(
                "INSERT INTO patterns (file_path, pattern_type, signal_name, handler, line, metadata) VALUES (?,?,?,?,?,?)",
                (rel_path, pat["pattern_type"], pat.get("signal_name"),
                 pat.get("handler"), pat.get("line"),
                 json.dumps(pat["metadata"]) if pat.get("metadata") else None)
            )

        return rel_path

    def _resolve_call_edges(self):
        fanout_cap = _env_int("RESOLVE_FANOUT_CAP", 10)
        score_same = _env_int("RESOLVE_SCORE_SAME_FILE", 2)
        score_import = _env_int("RESOLVE_SCORE_DIRECT_IMPORT", 1)
        score_global = _env_int("RESOLVE_SCORE_GLOBAL", 0)

        unresolved = self.db.execute(
            "SELECT id, source_file, callee FROM edges WHERE callee_qualified IS NULL"
        ).fetchall()

        for edge_id, source_file, callee_name in unresolved:
            imports_row = self.db.execute(
                "SELECT imports FROM files WHERE path = ?", (source_file,)
            ).fetchone()
            import_list = json.loads(imports_row[0]) if imports_row and imports_row[0] else []

            candidates = []

            same_file = self.db.execute(
                "SELECT file_path || '::' || name FROM symbols WHERE file_path = ? AND (name = ? OR short_name = ?)",
                (source_file, callee_name, callee_name)
            ).fetchall()
            for (q,) in same_file:
                candidates.append((q, score_same))

            if import_list:
                placeholders = ','.join(['?'] * len(import_list))
                import_syms = self.db.execute(
                    f"SELECT file_path || '::' || name FROM symbols WHERE file_path IN ({placeholders}) AND (name = ? OR short_name = ?)",
                    import_list + [callee_name, callee_name]
                ).fetchall()
                for (q,) in import_syms:
                    if not any(c[0] == q for c in candidates):
                        candidates.append((q, score_import))

            if not candidates:
                global_syms = self.db.execute(
                    "SELECT file_path || '::' || name FROM symbols WHERE (name = ? OR short_name = ?) AND file_path != ? LIMIT ?",
                    (callee_name, callee_name, source_file, fanout_cap)
                ).fetchall()
                for (q,) in global_syms:
                    candidates.append((q, score_global))

            if not candidates:
                continue

            candidates.sort(key=lambda x: x[1], reverse=True)
            best = candidates[0][0]
            best_file = best.split("::")[0] if "::" in best else None

            if len(candidates) > 1 and candidates[0][1] == candidates[1][1]:
                provenance = "speculative"
            elif candidates[0][1] >= score_import:
                provenance = "definite"
            else:
                provenance = "probable"

            self.db.execute(
                "UPDATE edges SET callee_qualified = ?, callee_file = ?, provenance = ? WHERE id = ?",
                (best, best_file, provenance, edge_id)
            )

            if len(candidates) > 1:
                for q, score in candidates[:fanout_cap]:
                    self.db.execute(
                        "INSERT OR IGNORE INTO edge_candidates (edge_id, candidate_qualified, score) VALUES (?,?,?)",
                        (edge_id, q, score)
                    )

        self.db.commit()

    def _run_synthesizers(self):
        from synthesizers import run_all_synthesizers
        run_all_synthesizers(self.db)

    def _purge_heuristic_edges(self, source_paths):
        if not source_paths:
            return
        placeholders = ','.join(['?'] * len(source_paths))
        self.db.execute(
            f"DELETE FROM edges WHERE provenance = 'inferred' AND synthesized_from IN ({placeholders})",
            list(source_paths)
        )

    def _compute_file_kinds(self):
        rows = self.db.execute(
            """SELECT f.path,
                      (SELECT COUNT(*) FROM symbols s WHERE s.file_path = f.path) AS sym_count,
                      (SELECT COUNT(*) FROM edges e WHERE e.source_file = f.path AND e.callee_file = f.path) AS intra_edges
               FROM files f"""
        ).fetchall()
        for path, sym_count, intra_edges in rows:
            hint = _compute_kind_hint(sym_count or 0, intra_edges or 0)
            self.db.execute(
                "UPDATE files SET kind_hint = ? WHERE path = ?",
                (hint, path)
            )
        self.db.commit()

    def _detect_clusters(self):
        density_threshold = _env_float("CLUSTER_DENSITY_THRESHOLD", 0.5)
        max_size = _env_int("CLUSTER_MAX_SIZE", 15)
        entry_count = _env_int("CLUSTER_ENTRY_COUNT", 3)

        all_paths = [r[0] for r in self.db.execute("SELECT path FROM files")]
        groups = {}
        for p in all_paths:
            parts = p.split("/")
            key = parts[0] if len(parts) > 1 else "_root"
            groups.setdefault(key, []).append(p)

        self.db.execute("DELETE FROM cluster_members")
        self.db.execute("DELETE FROM clusters")

        for gname, members in groups.items():
            if len(members) < 2:
                continue

            if len(members) > max_size:
                sub_groups = {}
                for p in members:
                    parts = p.split("/")
                    sub_key = "/".join(parts[:2]) if len(parts) > 2 else gname
                    sub_groups.setdefault(sub_key, []).append(p)
                final_groups = sub_groups
            else:
                final_groups = {gname: members}

            for cluster_name, cluster_files in final_groups.items():
                if len(cluster_files) < 2:
                    continue
                placeholders = ','.join(['?'] * len(cluster_files))
                edge_count = self.db.execute(
                    f"SELECT COUNT(*) FROM edges WHERE source_file IN ({placeholders}) AND callee_file IN ({placeholders})",
                    cluster_files + cluster_files
                ).fetchone()[0]
                density = edge_count / len(cluster_files)
                if density < density_threshold:
                    continue

                in_degree = self.db.execute(
                    f"""SELECT callee_qualified, COUNT(*) as cnt FROM edges
                        WHERE callee_file IN ({placeholders}) AND callee_qualified IS NOT NULL
                        GROUP BY callee_qualified ORDER BY cnt DESC LIMIT ?""",
                    cluster_files + [entry_count]
                ).fetchall()
                entry_symbols = [row[0] for row in in_degree]
                if not entry_symbols:
                    entry_symbols = [f"{cluster_files[0]}::*"]

                self.db.execute(
                    "INSERT INTO clusters (name, label, entry_symbols, file_count) VALUES (?,?,?,?)",
                    (cluster_name, None, json.dumps(entry_symbols), len(cluster_files))
                )
                cluster_id = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
                self.db.executemany(
                    "INSERT INTO cluster_members (cluster_id, file_path) VALUES (?,?)",
                    [(cluster_id, fp) for fp in cluster_files]
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO node_change_counters (node_kind, node_ref, child_change_count, leaf_descendant_count) VALUES (?,?,?,?)",
                    ('cluster', cluster_name, 0, 0)
                )

        existing_cluster_refs = {r[0] for r in self.db.execute("SELECT name FROM clusters")}
        stale = self.db.execute(
            "SELECT node_ref FROM node_change_counters WHERE node_kind = 'cluster'"
        ).fetchall()
        for (ref,) in stale:
            if ref not in existing_cluster_refs:
                self.db.execute(
                    "DELETE FROM node_change_counters WHERE node_kind = 'cluster' AND node_ref = ?",
                    (ref,)
                )

        self.db.commit()

    def _scan_one_file(self, full_path, parser, rel_path):
        savepoint = "scan_file"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self.scan_file(full_path, parser)
            if result is None:
                raise OSError(f"Unable to read source file: {rel_path}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result, None
        except Exception as exc:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return None, StageError("file_scan", str(exc), rel_path)

    def scan_all(self):
        batch_size = _env_int("SCAN_COMMIT_BATCH_SIZE", 100)
        discovered_paths = set()
        successful_paths = set()
        failed_paths = set()
        errors = []
        count = 0

        for root, dirs, files in os.walk(self.root_dir):
            dirs[:] = [d for d in dirs if not self._is_excluded(os.path.join(root, d))]
            for file in files:
                full_path = os.path.join(root, file)
                if self._is_excluded(full_path):
                    continue
                parser = self._get_parser_for_file(file)
                if not parser:
                    continue
                rel_path = os.path.relpath(full_path, self.root_dir).replace(os.sep, '/')
                discovered_paths.add(rel_path)
                result, error = self._scan_one_file(full_path, parser, rel_path)
                if error is not None:
                    failed_paths.add(rel_path)
                    errors.append(error)
                    continue
                successful_paths.add(result)
                count += 1
                if count % batch_size == 0:
                    self.db.commit()

        self.db.commit()

        db_paths = {r[0] for r in self.db.execute("SELECT path FROM files")}
        deleted = db_paths - discovered_paths
        if deleted:
            self.db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in deleted])
            self.db.commit()

        postprocess_complete = True
        for stage, operation in (
            ("resolve_edges", self._resolve_call_edges),
            ("synthesizers", self._run_synthesizers),
            ("file_kinds", self._compute_file_kinds),
            ("clusters", self._detect_clusters),
        ):
            try:
                operation()
            except Exception as exc:
                self.db.rollback()
                errors.append(StageError(stage, str(exc)))
                postprocess_complete = False
                break

        if postprocess_complete:
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
                (datetime.now().isoformat(timespec='seconds'),)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('file_count', ?)",
                (str(len(discovered_paths)),)
            )
            head, _ = _resolve_git_head(self.root_dir, self.db)
            if head:
                self.db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('source_commit', ?)",
                    (head,)
                )
            self.db.commit()

        return ScanResult.from_parts(
            discovered_paths=discovered_paths,
            successful_paths=successful_paths,
            failed_paths=failed_paths,
            deleted_paths=deleted,
            errors=errors,
            postprocess_complete=postprocess_complete,
        )

    def scan_files(self, file_paths):
        discovered_paths = set()
        successful_paths = set()
        failed_paths = set()
        deleted_paths = set()
        errors = []
        scanned_rel_paths = []
        for file_path in file_paths:
            if os.path.isabs(file_path):
                full_path = file_path
            else:
                full_path = os.path.join(self.root_dir, file_path)
            rel = os.path.relpath(full_path, self.root_dir).replace(os.sep, '/')
            discovered_paths.add(rel)

            if not os.path.exists(full_path):
                self.db.execute("DELETE FROM files WHERE path = ?", (rel,))
                successful_paths.add(rel)
                deleted_paths.add(rel)
                continue

            parser = self._get_parser_for_file(os.path.basename(full_path))
            if not parser:
                successful_paths.add(rel)
                continue

            result, error = self._scan_one_file(full_path, parser, rel)
            if error is not None:
                failed_paths.add(rel)
                errors.append(error)
                continue
            successful_paths.add(result)
            scanned_rel_paths.append(result)

        self.db.commit()
        postprocess_complete = True
        try:
            self._purge_heuristic_edges(scanned_rel_paths)

            if scanned_rel_paths:
                placeholders = ','.join(['?'] * len(scanned_rel_paths))
                affected_edges = self.db.execute(
                    f"""SELECT e.id FROM edges e
                        JOIN files f ON e.source_file = f.path
                        WHERE e.callee_qualified IS NOT NULL
                        AND e.callee_file IN ({placeholders})""",
                    scanned_rel_paths
                ).fetchall()
                if affected_edges:
                    edge_ids = [r[0] for r in affected_edges]
                    id_placeholders = ','.join(['?'] * len(edge_ids))
                    self.db.execute(
                        f"UPDATE edges SET callee_qualified = NULL, callee_file = NULL WHERE id IN ({id_placeholders})",
                        edge_ids
                    )
                    self.db.execute(
                        f"DELETE FROM edge_candidates WHERE edge_id IN ({id_placeholders})",
                        edge_ids
                    )

            self._resolve_call_edges()
            self._compute_file_kinds()
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
                (datetime.now().isoformat(timespec='seconds'),)
            )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            errors.append(StageError("incremental_postprocess", str(exc)))
            postprocess_complete = False

        return ScanResult.from_parts(
            discovered_paths=discovered_paths,
            successful_paths=successful_paths,
            failed_paths=failed_paths,
            deleted_paths=deleted_paths,
            errors=errors,
            postprocess_complete=postprocess_complete,
        )


def scan_all(root_dir, acquire_lock=True, lock_timeout=None, manage_dirty=False):
    lock = project_scan_lock(root_dir, timeout=lock_timeout) if acquire_lock else None
    queue = DirtyQueue(root_dir) if manage_dirty else None
    claim = None
    result = None
    try:
        if lock is not None:
            lock.acquire()
        if queue is not None:
            claim = queue.claim()
        scanner = StructScanner(root_dir)
        try:
            result = scanner.scan_all()
            return result
        finally:
            scanner.db.close()
    finally:
        if queue is not None and claim is not None:
            if result is not None and result.postprocess_complete:
                acknowledged = set(result.successful_paths) | set(result.deleted_paths)
                queue.finish(claim, acknowledged)
            else:
                queue.finish(claim, retry_all=True)
        if lock is not None:
            lock.release()


def scan_files(root_dir, file_paths, acquire_lock=True, lock_timeout=None, manage_dirty=False):
    lock = project_scan_lock(root_dir, timeout=lock_timeout) if acquire_lock else None
    queue = DirtyQueue(root_dir) if manage_dirty else None
    claim = None
    result = None
    try:
        if lock is not None:
            lock.acquire()
        if queue is not None:
            claim = queue.claim(file_paths)
            scan_targets = claim.paths
        else:
            scan_targets = file_paths
        if not scan_targets:
            return ScanResult.from_parts()
        scanner = StructScanner(root_dir)
        try:
            result = scanner.scan_files(scan_targets)
            return result
        finally:
            scanner.db.close()
    finally:
        if queue is not None and claim is not None:
            if result is not None and result.postprocess_complete:
                queue.finish(claim, result.successful_paths)
            else:
                queue.finish(claim, retry_all=True)
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    import argparse

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description="Structural scan for logic_index.db")
    ap.add_argument("--files", nargs="*", help="Incremental: only scan these files")
    ap.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    ap.add_argument("--lock-timeout", type=float, default=None,
                    help="Override project scan lock wait in seconds")
    ap.add_argument("--consume-dirty", action="store_true",
                    help="Claim and acknowledge matching dirty queue entries")
    args = ap.parse_args()

    try:
        if args.files:
            result = scan_files(
                args.cwd, args.files, lock_timeout=args.lock_timeout,
                manage_dirty=args.consume_dirty,
            )
        else:
            result = scan_all(
                args.cwd, lock_timeout=args.lock_timeout,
                manage_dirty=args.consume_dirty,
            )
    except LockTimeoutError as exc:
        print(f"Structural scan failed: {exc}", file=sys.stderr)
        sys.exit(RunStatus.FAILED.exit_code)

    for error in result.errors:
        location = f" ({error.path})" if error.path else ""
        print(f"[{error.stage}]{location} {error.message}", file=sys.stderr)
    print(f"STRUCT_SCAN_RESULT status={result.status.value} "
          f"successful={len(result.successful_paths)} failed={len(result.failed_paths)}")
    sys.exit(result.exit_code)
