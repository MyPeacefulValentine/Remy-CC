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
import sys
import sqlite3
import fnmatch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers.base import SymbolInfo
from parsers.python_parser import PythonParser
from parsers.c_cpp_parser import CCppParser
from parsers.ts_parser import TSParser

VERSION = "5.0.0"
DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")
JSON_CACHE_FILE = os.path.join(".claude", "logic_index.json")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT
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
    summary TEXT,
    bases TEXT,
    name_tokens TEXT NOT NULL DEFAULT '',
    UNIQUE(file_path, name)
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
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_short ON symbols(short_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_callee_q ON edges(callee_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(source_file, caller);
CREATE INDEX IF NOT EXISTS idx_edges_provenance ON edges(provenance);
CREATE INDEX IF NOT EXISTS idx_edges_source_file ON edges(source_file);
CREATE INDEX IF NOT EXISTS idx_patterns_type_signal ON patterns(pattern_type, signal_name);
CREATE INDEX IF NOT EXISTS idx_patterns_file ON patterns(file_path);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    name_tokens,
    file_path,
    summary,
    content='symbols',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS symbols_fts_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
    VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, name_tokens, file_path, summary)
    VALUES ('delete', OLD.id, OLD.name, OLD.name_tokens, OLD.file_path, OLD.summary);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, name_tokens, file_path, summary)
    VALUES ('delete', OLD.id, OLD.name, OLD.name_tokens, OLD.file_path, OLD.summary);
    INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
    VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
END;
"""


def tokenize_symbol(name):
    """Split snake_case, camelCase, and namespace separators into space-separated tokens."""
    s = name.replace("_", " ").replace("::", " ")
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
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
        needs_migration = (
            not os.path.exists(self.db_path)
            and os.path.exists(os.path.join(self.root_dir, JSON_CACHE_FILE))
        )
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA_SQL)

        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if version and version[0] != VERSION:
            db.close()
            os.remove(self.db_path)
            db = sqlite3.connect(self.db_path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA_SQL)
            version = None
            needs_migration = os.path.exists(os.path.join(self.root_dir, JSON_CACHE_FILE))

        if not version:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
            db.commit()

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
                        "INSERT OR IGNORE INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, summary, bases, name_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (path, sym["name"], short, sym.get("type", "function"),
                         sym.get("args"), sym.get("lineno"), sym.get("end_lineno"),
                         sym.get("hash"), sym.get("summary"), bases_json,
                         tokens)
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
        symbols = parser.parse_symbols(source, file_path)
        call_edges = parser.extract_call_graph(source, file_path)
        pattern_list = parser.extract_patterns(source, file_path)
        layer = self._match_file_to_layer(rel_path)

        self.db.execute(
            "INSERT OR REPLACE INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,?,?,?)",
            (rel_path, struct_hash, parser.__class__.__name__, layer, json.dumps(list(imports.keys())))
        )

        old_summaries = {}
        for row in self.db.execute(
            "SELECT name, hash, summary FROM symbols WHERE file_path = ?", (rel_path,)
        ):
            old_summaries[row[0]] = (row[1], row[2])

        self.db.execute("DELETE FROM symbols WHERE file_path = ?", (rel_path,))
        self.db.execute("DELETE FROM edges WHERE source_file = ?", (rel_path,))
        self.db.execute("DELETE FROM patterns WHERE file_path = ?", (rel_path,))

        for sym_info in symbols:
            stripped = self._strip_comments(sym_info.source_segment, parser)
            symbol_hash = self._calculate_symbol_hash(stripped)
            short_name = sym_info.name.split(".")[-1] if "." in sym_info.name else sym_info.name

            summary = None
            old = old_summaries.get(sym_info.name)
            if old and old[0] == symbol_hash:
                summary = old[1]

            if not summary:
                if sym_info.docstring:
                    lines = [line.strip() for line in sym_info.docstring.splitlines() if line.strip()]
                    if lines:
                        summary = "[Doc] " + " ".join(lines[:3])
                elif self.filter_small and len(sym_info.source_segment.splitlines()) < 3:
                    summary = "Small utility function."

            bases_json = json.dumps(sym_info.bases) if sym_info.bases else None
            tokens = tokenize_symbol(sym_info.name)
            self.db.execute(
                "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, end_lineno, hash, summary, bases, name_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rel_path, sym_info.name, short_name, sym_info.type,
                 sym_info.args, sym_info.lineno, sym_info.end_lineno,
                 symbol_hash, summary, bases_json, tokens)
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

            provenance = "ambiguous" if len(candidates) > 1 and candidates[0][1] == candidates[1][1] else None

            self.db.execute(
                "UPDATE edges SET callee_qualified = ?, callee_file = ?, provenance = COALESCE(provenance, ?) WHERE id = ?",
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
            f"DELETE FROM edges WHERE provenance = 'heuristic' AND synthesized_from IN ({placeholders})",
            list(source_paths)
        )

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

        self.db.commit()

    def scan_all(self):
        batch_size = _env_int("SCAN_COMMIT_BATCH_SIZE", 100)
        scanned_paths = set()
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
                result = self.scan_file(full_path, parser)
                if result:
                    scanned_paths.add(result)
                    count += 1
                    if count % batch_size == 0:
                        self.db.commit()

        self.db.commit()

        db_paths = {r[0] for r in self.db.execute("SELECT path FROM files")}
        deleted = db_paths - scanned_paths
        if deleted:
            self.db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in deleted])
            self.db.commit()

        self._resolve_call_edges()
        self._run_synthesizers()
        self._detect_clusters()

        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
            (datetime.now().isoformat(timespec='seconds'),)
        )
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('file_count', ?)",
            (str(len(scanned_paths)),)
        )
        self.db.commit()

    def scan_files(self, file_paths):
        scanned_rel_paths = []
        for file_path in file_paths:
            if os.path.isabs(file_path):
                full_path = file_path
            else:
                full_path = os.path.join(self.root_dir, file_path)

            if not os.path.exists(full_path):
                rel = os.path.relpath(full_path, self.root_dir).replace(os.sep, '/')
                self.db.execute("DELETE FROM files WHERE path = ?", (rel,))
                continue

            parser = self._get_parser_for_file(os.path.basename(full_path))
            if not parser:
                continue

            result = self.scan_file(full_path, parser)
            if result:
                scanned_rel_paths.append(result)

        self.db.commit()
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
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
            (datetime.now().isoformat(timespec='seconds'),)
        )
        self.db.commit()


def scan_all(root_dir):
    scanner = StructScanner(root_dir)
    scanner.scan_all()
    scanner.db.close()


def scan_files(root_dir, file_paths):
    scanner = StructScanner(root_dir)
    scanner.scan_files(file_paths)
    scanner.db.close()


if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description="Structural scan for logic_index.db")
    ap.add_argument("--files", nargs="*", help="Incremental: only scan these files")
    ap.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    args = ap.parse_args()

    if args.files:
        scan_files(args.cwd, args.files)
    else:
        scan_all(args.cwd)
