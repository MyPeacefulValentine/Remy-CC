"""Structural fact extraction and full/incremental scan execution."""

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime

from index_state import (
    DirtyQueue,
    ScanResult,
    StageError,
    project_scan_lock,
)
from migrations import initialize_database
from parsers.c_cpp_parser import CCppParser
from parsers.python_parser import PythonParser
from parsers.ts_parser import TSParser
from retrieval_projection import (
    delete_file_nodes,
    delete_node,
    mark_current_summary_stale,
    mark_node_and_ancestors_stale,
    refresh_node,
)
from symbol_names import tokenize_symbol
from symbol_selection import select_symbols


DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")


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

def _compute_kind_hint(sym_count, intra_edges):
    min_symbols = _env_int("FILE_KIND_MIN_SYMBOLS", 5)
    low_cohesion_threshold = _env_float("FILE_KIND_LOW_COHESION_THRESHOLD", 0.25)
    if sym_count < min_symbols:
        return "trivial"
    density = intra_edges / sym_count if sym_count else 0
    if density < low_cohesion_threshold:
        return "low_cohesion"
    return "cohesive"

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
        self.db = initialize_database(self.root_dir, self.db_path)

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

        old_hashes = {
            row[0]: row[1]
            for row in self.db.execute(
                "SELECT name, hash FROM symbols WHERE file_path = ?", (rel_path,)
            )
        }
        old_symbol_refs = {f"{rel_path}::{name}" for name in old_hashes}
        if existing:
            mark_node_and_ancestors_stale(self.db, "file", rel_path)
        existing_versions = {
            row[0]: row[1]
            for row in self.db.execute(
                "SELECT node_ref, MAX(version) FROM summary_versions "
                "WHERE node_kind = 'symbol' AND node_ref LIKE ? GROUP BY node_ref",
                (f"{rel_path}::%",),
            )
        }

        file_values = (
            struct_hash,
            parser.__class__.__name__,
            layer,
            json.dumps(list(imports.keys())),
            rel_path,
        )
        if existing:
            self.db.execute(
                "UPDATE files SET struct_hash=?, language=?, layer=?, imports=? "
                "WHERE path=?",
                file_values,
            )
        else:
            self.db.execute(
                "INSERT INTO files (path, struct_hash, language, layer, imports) "
                "VALUES (?,?,?,?,?)",
                (rel_path,) + file_values[:-1],
            )

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
        new_symbol_refs = set()
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
            new_symbol_refs.add(node_ref)
            hash_unchanged = old_hashes.get(sym_info.name) == symbol_hash
            has_existing_version = node_ref in existing_versions
            if hash_unchanged and has_existing_version:
                refresh_node(self.db, "symbol", node_ref)
                continue

            if has_existing_version:
                mark_node_and_ancestors_stale(self.db, "symbol", node_ref)

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
            refresh_node(self.db, "symbol", node_ref)

        for removed_ref in old_symbol_refs - new_symbol_refs:
            delete_node(self.db, "symbol", removed_ref)
        refresh_node(self.db, "file", rel_path)

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
            "SELECT id, source_file, callee FROM edges "
            "WHERE callee_qualified IS NULL "
            "AND (provenance != 'inferred' OR provenance IS NULL) "
            "ORDER BY source_file, caller, callee, COALESCE(line, 0), id"
        ).fetchall()

        for edge_id, source_file, callee_name in unresolved:
            imports_row = self.db.execute(
                "SELECT imports FROM files WHERE path = ?", (source_file,)
            ).fetchone()
            import_list = json.loads(imports_row[0]) if imports_row and imports_row[0] else []

            candidates = []

            same_file = self.db.execute(
                "SELECT file_path || '::' || name FROM symbols "
                "WHERE file_path = ? AND (name = ? OR short_name = ?) "
                "ORDER BY file_path, name",
                (source_file, callee_name, callee_name)
            ).fetchall()
            for (q,) in same_file:
                candidates.append((q, score_same))

            if import_list:
                placeholders = ','.join(['?'] * len(import_list))
                import_syms = self.db.execute(
                    f"SELECT file_path || '::' || name FROM symbols "
                    f"WHERE file_path IN ({placeholders}) "
                    "AND (name = ? OR short_name = ?) ORDER BY file_path, name",
                    import_list + [callee_name, callee_name]
                ).fetchall()
                for (q,) in import_syms:
                    if not any(c[0] == q for c in candidates):
                        candidates.append((q, score_import))

            if not candidates:
                global_syms = self.db.execute(
                    "SELECT file_path || '::' || name FROM symbols "
                    "WHERE (name = ? OR short_name = ?) AND file_path != ? "
                    "ORDER BY file_path, name LIMIT ?",
                    (callee_name, callee_name, source_file, fanout_cap)
                ).fetchall()
                for (q,) in global_syms:
                    candidates.append((q, score_global))

            if not candidates:
                continue

            candidates.sort(key=lambda item: (-item[1], item[0]))
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

    def _run_synthesizers(self):
        from synthesizers import run_all_synthesizers
        return run_all_synthesizers(self.db)

    def _purge_heuristic_edges(self):
        self.db.execute("DELETE FROM edges WHERE provenance = 'inferred'")

    def _reset_direct_edge_resolution(self):
        self.db.execute("DELETE FROM edge_candidates")
        self.db.execute(
            "UPDATE edges SET callee_qualified = NULL, callee_file = NULL, "
            "provenance = NULL WHERE provenance != 'inferred' OR provenance IS NULL"
        )

    def _run_postprocess(self):
        self._reset_direct_edge_resolution()
        self._resolve_call_edges()
        self._purge_heuristic_edges()
        synth_counts = self._run_synthesizers()
        self._compute_file_kinds()
        self._detect_clusters()
        return synth_counts

    def _compute_file_kinds(self):
        rows = self.db.execute(
            """SELECT f.path,
                      (SELECT COUNT(*) FROM symbols s WHERE s.file_path = f.path) AS sym_count,
                      (SELECT COUNT(*) FROM edges e WHERE e.source_file = f.path AND e.callee_file = f.path) AS intra_edges
               FROM files f ORDER BY f.path"""
        ).fetchall()
        for path, sym_count, intra_edges in rows:
            hint = _compute_kind_hint(sym_count or 0, intra_edges or 0)
            self.db.execute(
                "UPDATE files SET kind_hint = ? WHERE path = ?",
                (hint, path)
            )

    def _detect_clusters(self):
        density_threshold = _env_float("CLUSTER_DENSITY_THRESHOLD", 0.5)
        max_size = _env_int("CLUSTER_MAX_SIZE", 15)
        entry_count = _env_int("CLUSTER_ENTRY_COUNT", 3)

        all_paths = [r[0] for r in self.db.execute("SELECT path FROM files ORDER BY path")]
        groups = {}
        for p in all_paths:
            parts = p.split("/")
            key = parts[0] if len(parts) > 1 else "_root"
            groups.setdefault(key, []).append(p)

        existing_cluster_members = {
            name: frozenset(
                row[0]
                for row in self.db.execute(
                    "SELECT cm.file_path FROM cluster_members cm "
                    "JOIN clusters c ON c.id = cm.cluster_id WHERE c.name = ? "
                    "ORDER BY cm.file_path",
                    (name,),
                ).fetchall()
            )
            for (name,) in self.db.execute(
                "SELECT name FROM clusters ORDER BY name"
            ).fetchall()
        }
        existing_cluster_refs = set(existing_cluster_members)

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
                        GROUP BY callee_qualified
                        ORDER BY cnt DESC, callee_qualified ASC LIMIT ?""",
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
                if existing_cluster_members.get(cluster_name) != frozenset(cluster_files):
                    mark_current_summary_stale(self.db, "cluster", cluster_name)
                refresh_node(self.db, "cluster", cluster_name)

        current_cluster_refs = {
            r[0] for r in self.db.execute("SELECT name FROM clusters ORDER BY name")
        }
        for removed_ref in existing_cluster_refs - current_cluster_refs:
            delete_node(self.db, "cluster", removed_ref)
        stale = self.db.execute(
            "SELECT node_ref FROM node_change_counters "
            "WHERE node_kind = 'cluster' ORDER BY node_ref"
        ).fetchall()
        for (ref,) in stale:
            if ref not in current_cluster_refs:
                self.db.execute(
                    "DELETE FROM node_change_counters WHERE node_kind = 'cluster' AND node_ref = ?",
                    (ref,)
                )

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
            for path in deleted:
                symbol_refs = [
                    row[0]
                    for row in self.db.execute(
                        "SELECT file_path || '::' || name FROM symbols WHERE file_path = ?",
                        (path,),
                    ).fetchall()
                ]
                delete_file_nodes(self.db, path, symbol_refs)
            self.db.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in deleted])
            self.db.commit()

        postprocess_complete = True
        try:
            self._run_postprocess()
        except Exception as exc:
            self.db.rollback()
            errors.append(StageError("postprocess", str(exc)))
            postprocess_complete = False

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
            successful_paths=successful_paths if postprocess_complete else (),
            failed_paths=(failed_paths if postprocess_complete else discovered_paths),
            deleted_paths=deleted if postprocess_complete else (),
            errors=errors,
            postprocess_complete=postprocess_complete,
        )

    def scan_files(self, file_paths):
        discovered_paths = set()
        successful_paths = set()
        failed_paths = set()
        deleted_paths = set()
        errors = []
        transaction_targets = set()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for file_path in file_paths:
                if os.path.isabs(file_path):
                    full_path = file_path
                else:
                    full_path = os.path.join(self.root_dir, file_path)
                rel = os.path.relpath(full_path, self.root_dir).replace(os.sep, '/')
                discovered_paths.add(rel)
                transaction_targets.add(rel)

                if not os.path.exists(full_path):
                    symbol_refs = [
                        row[0]
                        for row in self.db.execute(
                            "SELECT file_path || '::' || name FROM symbols "
                            "WHERE file_path = ? ORDER BY name",
                            (rel,),
                        ).fetchall()
                    ]
                    delete_file_nodes(self.db, rel, symbol_refs)
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

            self._run_postprocess()
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)",
                (datetime.now().isoformat(timespec='seconds'),)
            )
            self.db.commit()
            postprocess_complete = True
        except Exception as exc:
            self.db.rollback()
            errors.append(StageError("incremental_postprocess", str(exc)))
            failed_paths.update(transaction_targets)
            successful_paths.clear()
            deleted_paths.clear()
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
