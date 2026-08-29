#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Logic Indexer - Generates semantic summaries for source code using AST/regex analysis and OpenAI-compatible API.
Features:
    - Multi-language support (Python, C, C++, TypeScript) via pluggable parsers
    - Incremental updates via MD5 hashing
    - Concurrent API calls (ThreadPoolExecutor)
    - Zero required external dependencies (Standard Library only; tree-sitter optional)
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

from parsers import build_default_registry
from llm_client import LlmClient, FatalError, TruncatedResponseError
import propagation
from schema import VERSION as SCHEMA_VERSION
from symbol_selection import select_symbols
from index_state import (
    LockTimeoutError,
    RunResult,
    RunStatus,
    ScanResult,
    StageError,
    project_scan_lock,
)
from retrieval_projection import (
    has_current_summary,
    select_current_summary,
)

VERSION = "4.0.0"

MAX_CTX_CHARS = 200000

DAEMON_BINARY_GUIDANCE = (
    "remy-cc binary not found; build it with 'cargo build --release' under "
    "remy-cc/ or reinstall Remy-CC so install.py deploys it to ~/.remy-cc/bin."
)


def find_daemon_binary():
    """Locate remy-cc: a development-tree build wins over the deployed copy."""
    name = "remy-cc.exe" if os.name == "nt" else "remy-cc"
    target_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "remy-cc", "target")
    )
    dev_builds = [
        path
        for path in (os.path.join(target_dir, profile, name) for profile in ("release", "debug"))
        if os.path.isfile(path)
    ]
    if dev_builds:
        return max(dev_builds, key=os.path.getmtime)
    remy_home = os.environ.get("REMY_CC_HOME") or os.path.join(os.path.expanduser("~"), ".remy-cc")
    deployed = os.path.join(remy_home, "bin", name)
    if os.path.isfile(deployed):
        return deployed
    return None


def run_daemon_scan(root_dir, db_path, config):
    """Run `remy-cc scan` and translate its terminal scan_result line."""
    binary = find_daemon_binary()
    if binary is None:
        return ScanResult(
            status=RunStatus.FAILED,
            errors=(StageError("struct_scan", DAEMON_BINARY_GUIDANCE),),
            postprocess_complete=False,
        )
    lock_timeout = config.get_float("REMY_INDEX_SCAN_LOCK_TIMEOUT")
    scan_timeout = config.get_int("REMY_STRUCT_SCAN_TIMEOUT")
    command = [
        binary, "scan", "--root", root_dir, "--db", db_path,
        "--result-json", "--lock-timeout", str(lock_timeout),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, scan_timeout + lock_timeout + 5.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ScanResult(
            status=RunStatus.FAILED,
            errors=(StageError("struct_scan", str(exc)),),
            postprocess_complete=False,
        )
    terminal = next(
        (line for line in reversed(completed.stdout.splitlines()) if line.strip()), ""
    )
    try:
        payload = json.loads(terminal)
    except json.JSONDecodeError:
        detail = completed.stderr.strip() or (
            f"scan exited {completed.returncode} without a scan_result line"
        )
        return ScanResult(
            status=RunStatus.FAILED,
            errors=(StageError("struct_scan", detail),),
            postprocess_complete=False,
        )
    status = {
        "success": RunStatus.SUCCESS,
        "partial": RunStatus.PARTIAL,
    }.get(payload.get("outcome"), RunStatus.FAILED)
    errors = tuple(
        StageError(item.get("stage", "struct_scan"), item.get("message", ""), item.get("path"))
        for item in payload.get("errors", [])
    )
    return ScanResult(
        status=status,
        successful_paths=tuple(payload.get("successful_paths", [])),
        failed_paths=tuple(payload.get("failed_paths", [])),
        deleted_paths=tuple(payload.get("deleted_paths", [])),
        errors=errors,
        postprocess_complete=bool(payload.get("postprocess_complete", False)),
    )


def open_semantic_connection(db_path):
    """Open the scanned database for the semantic layers; assert the schema version."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        row = connection.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise RuntimeError(f"logic index at {db_path} is unreadable: {exc}") from exc
    if row is None or row[0] != SCHEMA_VERSION:
        found = row[0] if row else "missing"
        connection.close()
        raise RuntimeError(
            f"logic index schema version {found} does not match {SCHEMA_VERSION}; "
            "rerun the structural scan"
        )
    return connection


class LogicIndexer:
    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)
        self.config = remy_config.load_config(self.root_dir, strict=True)

        self.llm_client = LlmClient(self.config)
        self.max_workers = self.config.get_int("REMY_LLM_MAX_WORKERS")

        self.db = None
        self.dirty_nodes = []
        self.summary_errors = []

        self._registry = build_default_registry()

        self.stats = {"start_time": time.time()}

    def _get_parser_for_file(self, filename):
        """Return the appropriate parser for a file, or None."""
        return self._registry.resolve(filename)

    def _load_prompt_template(self, parser):
        """Loads the prompt template for the given parser's language."""
        try:
            prompt_path = parser.get_prompt_template_path()
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return "Task: Summarize source code: {source_code}"

    def _get_dep_context_summaries(self, file_path):
        """Fetch dependency symbols' current short summary."""
        if not self.db:
            return []
        imports_row = self.db.execute(
            "SELECT imports FROM files WHERE path = ?", (file_path,)
        ).fetchone()
        if not imports_row or not imports_row[0]:
            return []
        try:
            import_list = json.loads(imports_row[0])
        except (json.JSONDecodeError, TypeError):
            return []
        if not import_list:
            return []
        placeholders = ','.join(['?'] * len(import_list))
        rows = self.db.execute(
            f"SELECT file_path, name FROM symbols "
            f"WHERE file_path IN ({placeholders})",
            import_list,
        ).fetchall()
        summaries = []
        for dep_file, name in rows:
            current = select_current_summary(
                self.db, "symbol", f"{dep_file}::{name}"
            )
            if current.get("short"):
                summaries.append((name, current["short"]))
        return summaries

    def _select_dirty_symbols(self):
        """Select symbols lacking a current usable summary."""
        if not self.db:
            return []
        rows = self.db.execute("SELECT file_path, name FROM symbols").fetchall()
        return [
            (file_path, name)
            for file_path, name in rows
            if not has_current_summary(
                self.db, "symbol", f"{file_path}::{name}"
            )
        ]

    def _persist_symbol_summaries(self, updates):
        """Insert status='ok' symbol summary versions.

        Delegates to summarizer.write_summary_version so the parent counter
        bump runs uniformly. Accepts iterable of (summary_text, file_path,
        symbol_name).
        """
        if not self.db or not updates:
            return
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from summarizer import write_summary_version
        except ImportError as exc:
            raise RuntimeError(
                f"summarizer unavailable; summary projection cannot be updated: {exc}"
            ) from exc

        for summary_text, file_path, sym_name in updates:
            node_ref = f"{file_path}::{sym_name}"
            payload = {"short": summary_text, "full": None}
            write_summary_version(self.db, "symbol", node_ref, payload, "ok")

    def _run_hierarchical_bootstrap(self, mode_override=None):
        """Run file/cluster summary bootstrap.

        Reads REMY_SUMMARY_BOOTSTRAP_MODE from the effective Remy configuration (auto/ask/never). Prints markers that
        the /remy-index SKILL.md consumes: BOOTSTRAP_RESULT for status, and
        BOOTSTRAP_PENDING_CONFIRMATION when ask-mode requires user input.
        Returns the result dict from bootstrap_summaries, or None when skipped.
        """
        if not self.db or self.llm_client.circuit_open:
            return None
        if not self.llm_client.api_key:
            print("Warning: REMY_LLM_API_KEY not configured; skipping file/cluster bootstrap.")
            return None
        try:
            from bootstrap import bootstrap_summaries
        except ImportError as exc:
            print(f"Warning: bootstrap module unavailable ({exc}); skipping file/cluster bootstrap.")
            return None

        print("\n[run] entering hierarchical bootstrap...", flush=True)
        try:
            result = bootstrap_summaries(self.db, self.llm_client.call, mode=mode_override)
        except Exception as exc:
            print(f"Error during hierarchical bootstrap: {exc}")
            return None

        mode = result.get("mode", "unknown")
        file_done = result.get("file_done", 0)
        cluster_done = result.get("cluster_done", 0)
        skipped = result.get("skipped", False)
        print("\n=== Hierarchical Summary Bootstrap ===")
        print(f"BOOTSTRAP_RESULT mode={mode} file_done={file_done} "
              f"cluster_done={cluster_done} skipped={skipped}")
        if result.get("needs_user_confirmation"):
            print(f"BOOTSTRAP_PENDING_CONFIRMATION "
                  f"pending_files={result.get('pending_files', 0)} "
                  f"pending_clusters={result.get('pending_clusters', 0)}")
        print("=" * 38)
        return result

    def _worker_task(self, file_path, items, context_summaries, parser):
        """Processes multiple symbols for a single file."""
        if self.llm_client.circuit_open:
            return

        try:
            with open(os.path.join(self.root_dir, file_path), 'r', encoding='utf-8') as f:
                source_code = f.read()
        except Exception as e:
            print(f"Error reading {file_path} for batch: {e}")
            return

        if len(source_code) / 3 > 30000:
            print(f"File {file_path} too large for batch. Falling back to atomic mode.")
            for symbol, segment in items:
                self._run_atomic_task(symbol, segment, context_summaries, parser)
            return

        target_names = [item[0]['name'] for item in items]
        prompt_template = self._load_prompt_template(parser)
        prompt = prompt_template.format(
            source_code=source_code,
            target_symbols=", ".join(target_names),
            context_summaries=context_summaries,
            lang=self.llm_client.lang
        )

        try:
            res = self.llm_client.call(prompt)
            if isinstance(res, str) and res.startswith("Error:"):
                message = f"API Error for {file_path}: {res}"
                print(message)
                self.summary_errors.append(StageError("symbol_summary", message, file_path))
                return

            summaries = json.loads(res)
            summary_map = {s['name']: s['summary'] for s in summaries if 'name' in s and 'summary' in s}
            for symbol, _ in items:
                if symbol['name'] in summary_map:
                    symbol['summary'] = summary_map[symbol['name']]
                else:
                    print(f"Warning: No summary returned for {symbol['name']} in {file_path}")

        except (json.JSONDecodeError, TruncatedResponseError) as e:
            print(f"Batch failed for {file_path} ({str(e)}). Switching to atomic mode...")
            for symbol, segment in items:
                self._run_atomic_task(symbol, segment, context_summaries, parser)
        except Exception as e:
            message = f"Error parsing batch response for {file_path}: {e}"
            print(message)
            self.summary_errors.append(StageError("symbol_summary", message, file_path))

    def _run_atomic_task(self, symbol, segment, context_summaries, parser):
        """Runs a single symbol task (Atomic Mode)."""
        prompt_template = self._load_prompt_template(parser)
        prompt = prompt_template.format(
            source_code=segment,
            target_symbols=symbol['name'],
            context_summaries=context_summaries,
            lang=self.llm_client.lang
        )
        try:
            res = self.llm_client.call(prompt)
            data = json.loads(res)
            symbol['summary'] = data[0]['summary'] if isinstance(data, list) else data['summary']
        except Exception as exc:
            symbol['summary'] = None
            self.summary_errors.append(
                StageError("symbol_summary", str(exc), symbol.get("name"))
            )

    def process_llm_queue(self):
        """Process dirty nodes grouped by file to minimize API calls."""
        if not self.dirty_nodes:
            return

        batches = {}
        parser_map = {}
        for file_path, symbol, segment, parser in self.dirty_nodes:
            if file_path not in batches:
                batches[file_path] = []
                parser_map[file_path] = parser
            batches[file_path].append((symbol, segment))

        print(f"Generating summaries for {len(self.dirty_nodes)} symbols across {len(batches)} files (Workers: {self.max_workers})...")

        batch_args = []
        for fp, items in batches.items():
            ctx_summary = ""
            dep_syms = self._get_dep_context_summaries(fp)
            if dep_syms:
                dep_list = []
                current_chars = 0
                for s_name, s_summary in dep_syms:
                    line = f"- {s_name}: {s_summary}"
                    if current_chars + len(line) + 1 > MAX_CTX_CHARS:
                        break
                    dep_list.append(line)
                    current_chars += len(line) + 1
                ctx_summary = "\n".join(dep_list)
            batch_args.append((fp, items, ctx_summary, parser_map[fp]))

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(self._worker_task, *args) for args in batch_args]
            try:
                for future in concurrent.futures.as_completed(futures):
                    if self.llm_client.circuit_open:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        future.result()
                        print(".", end="", flush=True)
                    except FatalError as e:
                        print(f"\n{e}")
                        self.summary_errors.append(StageError("symbol_summary", str(e)))
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    except Exception as e:
                        message = f"Error processing file batch: {e}"
                        print(message)
                        self.summary_errors.append(StageError("symbol_summary", message))
            except KeyboardInterrupt:
                print("\nInterrupted by user. Shutting down...")
                executor.shutdown(wait=False, cancel_futures=True)
                raise

    def run(self):
        print("Scanning codebase...")
        errors = []
        scan_result = None
        symbol_requested = 0
        symbol_completed = 0
        file_requested = 0
        file_completed = 0
        cluster_requested = 0
        cluster_completed = 0
        db_path = str(self.config.get("REMY_LOGIC_INDEX_DB_PATH"))
        lock = project_scan_lock(self.root_dir)

        try:
            scan_result = run_daemon_scan(self.root_dir, db_path, self.config)
            errors.extend(scan_result.errors)
            if scan_result.status == RunStatus.FAILED:
                return RunResult(RunStatus.FAILED, scan=scan_result, errors=tuple(errors))
            print("Structural scan complete.", flush=True)

            lock.acquire()
            self.db = open_semantic_connection(db_path)

            dirty_rows = self._select_dirty_symbols()
            symbol_requested = len(dirty_rows)
            dirty_by_file = {}
            for fpath, sym_name in dirty_rows:
                dirty_by_file.setdefault(fpath, []).append(sym_name)
            print(f"Symbol layer: {len(dirty_rows)} symbol(s) across {len(dirty_by_file)} file(s) need LLM summary.", flush=True)

            for path, sym_names in dirty_by_file.items():
                full_path = os.path.join(self.root_dir, path)
                parser = self._get_parser_for_file(os.path.basename(path))
                if not parser:
                    errors.append(StageError("symbol_summary", "No parser available", path))
                    continue
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        source = f.read()
                    selection = select_symbols(parser.parse_symbols(source, full_path))
                except Exception as exc:
                    errors.append(StageError("symbol_summary", str(exc), path))
                    continue
                seg_map = {
                    symbol.name: symbol.source_segment
                    for symbol in selection.canonical_symbols
                }
                for sym_name in sym_names:
                    segment = seg_map.get(sym_name, "")
                    if segment:
                        sym_dict = {"name": sym_name, "summary": None}
                        self.dirty_nodes.append((path, sym_dict, segment, parser))
                    else:
                        errors.append(StageError("symbol_summary", "Canonical source segment unavailable", f"{path}::{sym_name}"))

            if self.dirty_nodes:
                if not self.llm_client.api_key:
                    message = "REMY_LLM_API_KEY not found; symbol summaries remain pending."
                    print(f"Warning: {message}")
                    errors.append(StageError("symbol_summary", message))
                else:
                    print(f"Symbol layer: dispatching {len(self.dirty_nodes)} segment(s) to LLM (max_workers={self.max_workers})...", flush=True)
                    self.process_llm_queue()
                    errors.extend(self.summary_errors)

                    updates = [
                        (sym["summary"], path, sym["name"])
                        for path, sym, _segment, _parser in self.dirty_nodes
                        if sym.get("summary")
                    ]
                    if updates:
                        self._persist_symbol_summaries(updates)
                    symbol_completed = len(updates)
                    if symbol_completed < symbol_requested:
                        errors.append(StageError(
                            "symbol_summary",
                            f"Completed {symbol_completed} of {symbol_requested} requested summaries",
                        ))
                    print(f"\nSymbol layer: persisted {len(updates)} summaries.", flush=True)
            else:
                print("Symbol layer: no dirty symbols, skipping LLM phase.", flush=True)

            bootstrap_result = self._run_hierarchical_bootstrap()
            if bootstrap_result:
                file_requested = bootstrap_result.get("file_requested", 0)
                file_completed = bootstrap_result.get("file_done", 0)
                cluster_requested = bootstrap_result.get("cluster_requested", 0)
                cluster_completed = bootstrap_result.get("cluster_done", 0)
                if bootstrap_result.get("file_failed", 0) or bootstrap_result.get("cluster_failed", 0):
                    errors.append(StageError("hierarchical_summary", "One or more requested summaries remain incomplete"))
            elif self.db and not self.llm_client.api_key:
                from bootstrap import _pending_clusters, _pending_files
                pending_files = len(_pending_files(self.db))
                pending_clusters = len(_pending_clusters(self.db))
                if pending_files or pending_clusters:
                    file_requested = pending_files
                    cluster_requested = pending_clusters
                    errors.append(StageError(
                        "hierarchical_summary",
                        "Auto bootstrap could not run without REMY_LLM_API_KEY",
                    ))

            propagation_result = propagation.run_propagation_pass(self.db, self.llm_client)
            if self.llm_client.api_key and not self.llm_client.circuit_open:
                if propagation_result is None:
                    errors.append(StageError("propagation", "Propagation pass did not complete"))
                elif propagation_result.get("errors", 0):
                    errors.append(StageError(
                        "propagation",
                        f"{propagation_result['errors']} propagation operation(s) failed",
                    ))

            if scan_result.status == RunStatus.PARTIAL or errors:
                status = RunStatus.PARTIAL
            else:
                status = RunStatus.SUCCESS
            return RunResult(
                status,
                scan=scan_result,
                errors=tuple(errors),
                symbol_requested=symbol_requested,
                symbol_completed=symbol_completed,
                file_requested=file_requested,
                file_completed=file_completed,
                cluster_requested=cluster_requested,
                cluster_completed=cluster_completed,
            )
        except LockTimeoutError as exc:
            errors.append(StageError("scan_lock", str(exc)))
            return RunResult(RunStatus.FAILED, scan=scan_result, errors=tuple(errors))
        except Exception as exc:
            errors.append(StageError("run", str(exc)))
            status = RunStatus.PARTIAL if scan_result and scan_result.successful_paths else RunStatus.FAILED
            return RunResult(status, scan=scan_result, errors=tuple(errors))
        finally:
            duration = time.time() - self.stats["start_time"]
            file_count = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] if self.db else 0
            print("\n=== Logic Indexer Stats ===")
            print(f"Files in Index      : {file_count}")
            print(f"Symbols for LLM     : {len(self.dirty_nodes)}")
            print(f"API Calls           : {self.llm_client.api_calls}")
            print(f"Total Duration      : {duration:.2f}s")
            print("===========================\n")
            if self.db:
                self.db.close()
            lock.release()


if __name__ == "__main__":
    import argparse

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description="Logic Indexer: structural scan + LLM summaries.")
    ap.add_argument("--bootstrap-only", action="store_true",
                    help="Skip symbol-layer LLM; trigger only file/cluster bootstrap.")
    ap.add_argument("--mode", choices=["auto", "ask", "never"], default=None,
                    help="Override REMY_SUMMARY_BOOTSTRAP_MODE for this invocation.")
    cli_args = ap.parse_args()

    indexer = LogicIndexer(os.getcwd())
    if cli_args.bootstrap_only:
        db_path = str(indexer.config.get("REMY_LOGIC_INDEX_DB_PATH"))
        try:
            scan_result = run_daemon_scan(indexer.root_dir, db_path, indexer.config)
            if scan_result.status == RunStatus.FAILED:
                for error in scan_result.errors:
                    location = f" ({error.path})" if error.path else ""
                    print(f"[{error.stage}]{location} {error.message}", file=sys.stderr)
                sys.exit(RunStatus.FAILED.exit_code)
            with project_scan_lock(indexer.root_dir):
                indexer.db = open_semantic_connection(db_path)
                result = indexer._run_hierarchical_bootstrap(mode_override=cli_args.mode)
                status = scan_result.status
                if result and (result.get("file_failed", 0) or result.get("cluster_failed", 0)):
                    status = RunStatus.PARTIAL
        except LockTimeoutError as exc:
            print(f"[scan_lock] {exc}", file=sys.stderr)
            sys.exit(RunStatus.FAILED.exit_code)
        finally:
            if indexer.db:
                indexer.db.close()
        sys.exit(status.exit_code)
    else:
        result = indexer.run()
        for error in result.errors:
            location = f" ({error.path})" if error.path else ""
            print(f"[{error.stage}]{location} {error.message}", file=sys.stderr)
        if result.status == RunStatus.SUCCESS:
            print("\nLogic index updated.")
        else:
            print(f"\nLOGIC_INDEX_RESULT status={result.status.value}", file=sys.stderr)
        sys.exit(result.exit_code)
