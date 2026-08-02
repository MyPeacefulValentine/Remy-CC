#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Logic Indexer - Generates semantic summaries for source code using AST/regex analysis and OpenAI-compatible API.
Features:
    - Multi-language support (Python, C, C++, TypeScript) via pluggable parsers
    - Incremental updates via MD5 hashing
    - Concurrent API calls (ThreadPoolExecutor)
    - Zero required external dependencies (Standard Library only; tree-sitter optional)
Version: 3.0.0
"""

import json
import os
import sys
import subprocess
import time
import random
import concurrent.futures
import urllib.request
import urllib.error
import ssl
import fnmatch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

from parsers.python_parser import PythonParser
from parsers.c_cpp_parser import CCppParser
from parsers.ts_parser import TSParser
from struct_scan import StructScanner
from symbol_selection import select_symbols
from index_state import (
    DirtyQueue,
    LockTimeoutError,
    RunResult,
    RunStatus,
    StageError,
    project_scan_lock,
)
from retrieval_projection import (
    AVAILABLE_SUMMARY_STATUSES,
    has_current_summary,
    select_current_summary,
)

VERSION = "4.0.0"
DIRTY_FILE = os.path.join(".claude", "logic_index_dirty")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")

DEFAULT_LANG = "English"
MAX_CTX_CHARS = 200000

DEFAULT_AUTO_INJECT = "ALWAYS"
DEFAULT_FILTER_SMALL = False

DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 60.0


class FatalError(Exception):
    """Triggers circuit breaker and halts execution."""
    pass


class TruncatedResponseError(Exception):
    """Raised when API response is incomplete/truncated."""
    pass


class LogicIndexer:
    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)
        self.config = remy_config.load_config(self.root_dir, strict=True)

        self.api_key = self.config.get("REMY_LLM_API_KEY")
        self.model = self.config.get("REMY_LLM_MODEL")
        self.base_url = self.config.get("REMY_LLM_BASE_URL")
        self.circuit_open = False
        self.max_workers = self.config.get_int("REMY_LLM_MAX_WORKERS")
        self.max_tokens = self.config.get_int("REMY_LLM_MAX_TOKENS")
        self.retry_limit = self.config.get_int("REMY_LLM_RETRY_LIMIT")
        self.timeout = self.config.get_int("REMY_LLM_TIMEOUT")
        self.filter_small = self.config.get_bool("REMY_LOGIC_INDEX_FILTER_SMALL")
        remy_lang = self.config.get("REMY_LANG", "en")
        self.lang = {"zh-CN": "Simplified Chinese", "en": "English"}.get(remy_lang, DEFAULT_LANG)

        self.exclusions = []
        self.layers = []
        self._load_config()
        self.db = None
        self.dirty_nodes = []
        self.summary_errors = []

        self.parsers = [PythonParser(), CCppParser(), TSParser()]
        self._extension_map = {}
        for parser in self.parsers:
            for ext in parser.get_extensions():
                self._extension_map[ext] = parser

        self.stats = {
            "start_time": time.time(),
            "total_files": 0,
            "processed_files": 0,
            "api_calls": 0,
            "failed_files": 0,
            "token_usage_estimate": 0,
            "languages": {},
        }

        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def _get_parser_for_file(self, filename):
        """Return the appropriate parser for a file, or None."""
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
                    print(f"Initialized logic config at {CONFIG_FILE}")
            except Exception as e:
                print(f"Warning: Failed to create default config: {e}")

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

    def _is_path_excluded(self, rel_path):
        """Check if a relative file path matches exclusion rules, including parent directory patterns."""
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

    def _call_llm(self, prompt):
        if not self.api_key:
            return "Error: REMY_LLM_API_KEY not set."

        if self.circuit_open:
            return "Error: Circuit breaker open."

        url = self.base_url
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"You are a code analysis assistant. Respond in {self.lang}. Respond with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}
        }

        self.stats["api_calls"] += 1
        retries = 0
        while retries <= self.retry_limit:
            try:
                req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
                with urllib.request.urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                    raw_data = response.read().decode('utf-8')
                    result = json.loads(raw_data)
                    try:
                        text_content = result['choices'][0]['message']['content'].strip()

                        if "```json" in text_content:
                            text_content = text_content.split("```json")[1].split("```")[0].strip()
                        elif "```" in text_content:
                            text_content = text_content.split("```")[1].split("```")[0].strip()

                        if not text_content.strip().endswith(('}', ']')):
                            raise TruncatedResponseError("Response truncated (incomplete JSON)")

                        try:
                            json.loads(text_content)
                            return text_content
                        except json.JSONDecodeError:
                            pass
                        return text_content
                    except (KeyError, IndexError):
                        print(f"API Debug - Response Structure: {json.dumps(result)[:500]}")
                        return "Error: Unexpected API response format."
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    self.circuit_open = True
                    raise FatalError(f"Fatal API Error {e.code}: {e.reason}")

                if e.code in (500, 502, 503, 504) and retries < self.retry_limit:
                    retries += 1
                    wait = min(DEFAULT_RETRY_BACKOFF_CAP_SECONDS, 2 ** retries) + (random.random() * 0.3)
                    time.sleep(wait)
                    continue
                return f"Error: HTTP {e.code} - {e.reason}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if retries < self.retry_limit:
                    retries += 1
                    wait = min(DEFAULT_RETRY_BACKOFF_CAP_SECONDS, 2 ** retries) + (random.random() * 0.3)
                    time.sleep(wait)
                    continue
                return f"Error: Network error ({str(e)})"
            except TruncatedResponseError:
                if retries < self.retry_limit:
                    print(f"Warning: Response truncated. Retrying ({retries+1}/{self.retry_limit})...")
                    retries += 1
                    continue
                raise
            except Exception as e:
                return f"Error: {str(e)}"
        return "Error: Maximum retries exceeded."

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
        if not self.db or self.circuit_open:
            return None
        if not self.api_key:
            print("Warning: REMY_LLM_API_KEY not configured; skipping file/cluster bootstrap.")
            return None
        try:
            from bootstrap import bootstrap_summaries
        except ImportError as exc:
            print(f"Warning: bootstrap module unavailable ({exc}); skipping file/cluster bootstrap.")
            return None

        print("\n[run] entering hierarchical bootstrap...", flush=True)
        try:
            result = bootstrap_summaries(self.db, self._call_llm, mode=mode_override)
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

    @staticmethod
    def _env_int(name, default):
        key = name if name.startswith("REMY_") else "REMY_" + name
        try:
            return remy_config.load_config(strict=True).get_int(key)
        except (KeyError, TypeError, remy_config.ConfigError):
            return default

    def _force_recompute_check(self, parent_kind, parent_ref):
        """Return True when THRESHOLD_PRIMARY / THRESHOLD_BACKUP / INTERVAL_DAYS fires."""
        if not self.db:
            return False
        row = self.db.execute(
            "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind = ? AND node_ref = ?",
            (parent_kind, parent_ref),
        ).fetchone()
        if not row:
            return False
        child_cnt, leaf_cnt, last_force = row
        threshold_primary = self._env_int("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", 50)
        threshold_backup = self._env_int("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", -1)
        interval_days = self._env_int("REMY_FORCE_RECOMPUTE_INTERVAL_DAYS", 30)
        if threshold_primary > 0 and child_cnt >= threshold_primary:
            return True
        if threshold_backup >= 0 and leaf_cnt >= threshold_backup:
            return True
        if last_force and interval_days > 0:
            try:
                from datetime import timedelta
                elapsed = datetime.now() - datetime.fromisoformat(last_force)
                if elapsed >= timedelta(days=interval_days):
                    return True
            except (ValueError, TypeError):
                pass
        return False

    def _zero_counter(self, parent_kind, parent_ref, mark_force=False):
        """Reset child_change_count and leaf_descendant_count for a node."""
        if not self.db:
            return
        if mark_force:
            self.db.execute(
                "UPDATE node_change_counters SET child_change_count = 0, "
                "leaf_descendant_count = 0, last_force_recompute_at = ? "
                "WHERE node_kind = ? AND node_ref = ?",
                (datetime.now().isoformat(timespec='seconds'), parent_kind, parent_ref),
            )
        else:
            self.db.execute(
                "UPDATE node_change_counters SET child_change_count = 0, "
                "leaf_descendant_count = 0 "
                "WHERE node_kind = ? AND node_ref = ?",
                (parent_kind, parent_ref),
            )
        self.db.commit()

    def _collect_propagation_candidates(self, parent_kind):
        """Return parents with a current summary and child_change_count > 0."""
        if not self.db:
            return []
        rows = self.db.execute(
            "SELECT node_ref, child_change_count FROM node_change_counters "
            "WHERE node_kind = ? AND child_change_count > 0",
            (parent_kind,),
        ).fetchall()
        return [
            (node_ref, count)
            for node_ref, count in rows
            if has_current_summary(self.db, parent_kind, node_ref)
        ]

    def _get_latest_ok_summary(self, node_kind, node_ref):
        """Return the current usable summary payload, or None."""
        if not self.db:
            return None
        current = select_current_summary(self.db, node_kind, node_ref)
        if current.get("id") is None:
            return None
        return {"short": current.get("short"), "full": current.get("full")}

    def _build_child_changes_payload(self, parent_kind, parent_ref):
        """Assemble {child_ref, old_summary, new_summary} list for judge_propagation.

        Children are determined structurally:
            parent_kind='file'    -> children = symbols in that file
            parent_kind='cluster' -> children = files in that cluster
        old_summary uses the second-most-recent ok version when present.
        """
        if not self.db:
            return []
        if parent_kind == "file":
            rows = self.db.execute(
                "SELECT name FROM symbols WHERE file_path = ?", (parent_ref,)
            ).fetchall()
            child_kind = "symbol"
            child_refs = [f"{parent_ref}::{r[0]}" for r in rows]
        elif parent_kind == "cluster":
            rows = self.db.execute(
                """SELECT cm.file_path FROM cluster_members cm
                   JOIN clusters c ON cm.cluster_id = c.id
                   WHERE c.name = ?""",
                (parent_ref,),
            ).fetchall()
            child_kind = "file"
            child_refs = [r[0] for r in rows]
        else:
            return []

        changes = []
        for child_ref in child_refs:
            current = select_current_summary(self.db, child_kind, child_ref)
            if current.get("id") is None:
                continue
            new_summary = {
                "short": current.get("short"),
                "full": current.get("full"),
            }
            previous_rows = self.db.execute(
                "SELECT summary FROM summary_versions "
                "WHERE node_kind = ? AND node_ref = ? "
                "AND status IN ('ok', 'oversized_warn') AND version < ? "
                "ORDER BY version DESC LIMIT 1",
                (child_kind, child_ref, current["version"]),
            ).fetchall()
            old_summary = None
            if previous_rows and previous_rows[0][0]:
                try:
                    old_summary = json.loads(previous_rows[0][0])
                except (json.JSONDecodeError, TypeError):
                    old_summary = None
            if new_summary == old_summary:
                continue
            changes.append({
                "child_ref": child_ref,
                "old_summary": old_summary,
                "new_summary": new_summary,
            })
        return changes

    def _rewrite_parent_summary(self, parent_kind, parent_ref):
        """Regenerate a parent summary and return whether an ok version was written."""
        if not self.db:
            return False
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import summarizer
        except ImportError as exc:
            print(f"Warning: summarizer unavailable ({exc}); cannot rewrite {parent_kind} {parent_ref}.")
            return False
        if parent_kind == "file":
            row = self.db.execute(
                "SELECT kind_hint FROM files WHERE path = ?", (parent_ref,)
            ).fetchone()
            hint = row[0] if row else None
            payload, status = summarizer.summarize_file(self.db, parent_ref, hint, self._call_llm)
        elif parent_kind == "cluster":
            payload, status = summarizer.summarize_cluster(self.db, parent_ref, self._call_llm)
        else:
            return False
        if payload is None or status not in AVAILABLE_SUMMARY_STATUSES:
            return False
        summarizer.write_summary_version(
            self.db, parent_kind, parent_ref, payload, status
        )
        return True

    def _run_propagation_pass(self):
        """Run propagation judgment for file then cluster level.

        For each candidate (parent with ok summary AND child_change_count > 0):
        - If force-recompute fires: rewrite parent + zero counter + stamp last_force.
        - Else: call judge_propagation; propagate=true → rewrite + zero counter,
          propagate=false → keep counter (accumulates toward THRESHOLD_PRIMARY).
        """
        if not self.db or self.circuit_open or not self.api_key:
            return None
        print("\n[run] entering propagation pass...", flush=True)
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from llm_judge import judge_propagation
        except ImportError as exc:
            print(f"Warning: llm_judge unavailable ({exc}); skipping propagation pass.")
            return None

        stats = {
            "file_force": 0, "file_propagate": 0, "file_skip": 0,
            "cluster_force": 0, "cluster_propagate": 0, "cluster_skip": 0,
            "errors": 0,
        }
        for parent_kind in ("file", "cluster"):
            candidates = self._collect_propagation_candidates(parent_kind)
            for parent_ref, _child_cnt in candidates:
                if self.circuit_open:
                    break
                if self._force_recompute_check(parent_kind, parent_ref):
                    if self._rewrite_parent_summary(parent_kind, parent_ref):
                        self._zero_counter(parent_kind, parent_ref, mark_force=True)
                        stats[f"{parent_kind}_force"] += 1
                    else:
                        stats[f"{parent_kind}_skip"] += 1
                        stats["errors"] += 1
                    continue
                parent_prev = self._get_latest_ok_summary(parent_kind, parent_ref)
                child_changes = self._build_child_changes_payload(parent_kind, parent_ref)
                if not child_changes:
                    stats[f"{parent_kind}_skip"] += 1
                    continue
                try:
                    verdict = judge_propagation(
                        self.db, parent_kind, parent_ref, parent_prev,
                        child_changes, self._call_llm,
                    )
                except Exception as exc:
                    print(f"Error judging {parent_kind} {parent_ref}: {exc}")
                    stats[f"{parent_kind}_skip"] += 1
                    stats["errors"] += 1
                    continue
                if verdict.get("propagate"):
                    if self._rewrite_parent_summary(parent_kind, parent_ref):
                        self._zero_counter(parent_kind, parent_ref)
                        stats[f"{parent_kind}_propagate"] += 1
                    else:
                        stats[f"{parent_kind}_skip"] += 1
                        stats["errors"] += 1
                else:
                    stats[f"{parent_kind}_skip"] += 1

        print("\n=== Propagation Pass ===")
        print(
            "PROPAGATION_RESULT "
            f"file_propagate={stats['file_propagate']} file_skip={stats['file_skip']} "
            f"file_force={stats['file_force']} "
            f"cluster_propagate={stats['cluster_propagate']} cluster_skip={stats['cluster_skip']} "
            f"cluster_force={stats['cluster_force']}"
        )
        print("=" * 25)
        return stats

    def _worker_task(self, file_path, items, context_summaries, parser):
        """Processes multiple symbols for a single file."""
        if self.circuit_open:
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
            lang=self.lang
        )

        try:
            res = self._call_llm(prompt)
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
            lang=self.lang
        )
        try:
            res = self._call_llm(prompt)
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
                    if self.circuit_open:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    try:
                        future.result()
                        print(".", end="", flush=True)
                    except FatalError as e:
                        print(f"\n{e}")
                        self.summary_errors.append(StageError("symbol_summary", str(e)))
                        self.circuit_open = True
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
        queue = DirtyQueue(self.root_dir)
        claim = None
        lock = project_scan_lock(self.root_dir)

        try:
            lock.acquire()
            claim = queue.claim()
            scanner = StructScanner(self.root_dir)
            scan_result = scanner.scan_all()
            self.db = scanner.db
            errors.extend(scan_result.errors)
            if scan_result.status == RunStatus.FAILED:
                return RunResult(RunStatus.FAILED, scan=scan_result, errors=tuple(errors))
            print("Structural scan complete.", flush=True)

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
                if not self.api_key:
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
            elif self.db and not self.api_key:
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

            propagation_result = self._run_propagation_pass()
            if self.api_key and not self.circuit_open:
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
            if claim is not None:
                if scan_result is not None and scan_result.postprocess_complete:
                    queue.finish(claim, claim.paths)
                else:
                    queue.finish(claim, retry_all=True)
            duration = time.time() - self.stats["start_time"]
            file_count = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] if self.db else 0
            print("\n=== Logic Indexer Stats ===")
            print(f"Files in Index      : {file_count}")
            print(f"Symbols for LLM     : {len(self.dirty_nodes)}")
            print(f"API Calls           : {self.stats['api_calls']}")
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
        try:
            with project_scan_lock(indexer.root_dir):
                scanner = StructScanner(indexer.root_dir)
                scan_result = scanner.scan_all()
                indexer.db = scanner.db
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
