#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Logic Indexer - Generates semantic summaries for source code using AST/regex analysis and OpenAI-compatible API.
Features:
    - Multi-language support (Python, C, C++, TypeScript) via pluggable parsers
    - Incremental updates via MD5 hashing
    - Concurrent API calls (ThreadPoolExecutor)
    - Zero required external dependencies (Standard Library only; tree-sitter optional)
Version: 2.0.0
"""

import hashlib
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
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers.base import LanguageParser, SymbolInfo
from parsers.python_parser import PythonParser
from parsers.c_cpp_parser import CCppParser
from parsers.ts_parser import TSParser

VERSION = "3.0.0"
CACHE_FILE = os.path.join(".claude", "logic_index.json")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")
OUTPUT_MD = os.path.join(".claude", "logic_tree.md")

DEFAULT_MODEL = "glm-5"
DEFAULT_API_URL = "https://coding.dashscope.aliyuncs.com/v1/chat/completions"
DEFAULT_MAX_WORKERS = 5
DEFAULT_RETRY_LIMIT = 3
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_TOKENS = 8192
DEFAULT_LANG = "English"
MAX_CTX_CHARS = 200000

DEFAULT_AUTO_INJECT = "ALWAYS"
DEFAULT_FILTER_SMALL = False


class FatalError(Exception):
    """Triggers circuit breaker and halts execution."""
    pass


class TruncatedResponseError(Exception):
    """Raised when API response is incomplete/truncated."""
    pass


class LogicIndexer:
    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)

        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        self.base_url = os.environ.get("OPENAI_BASE_URL", DEFAULT_API_URL)
        self.circuit_open = False

        try:
            self.max_workers = int(os.environ.get("OPENAI_MAX_WORKERS", DEFAULT_MAX_WORKERS))
        except ValueError:
            self.max_workers = DEFAULT_MAX_WORKERS

        try:
            self.max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", DEFAULT_MAX_TOKENS))
        except ValueError:
            self.max_tokens = DEFAULT_MAX_TOKENS

        try:
            self.retry_limit = int(os.environ.get("OPENAI_RETRY_LIMIT", DEFAULT_RETRY_LIMIT))
        except ValueError:
            self.retry_limit = DEFAULT_RETRY_LIMIT

        try:
            self.timeout = int(os.environ.get("OPENAI_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError:
            self.timeout = DEFAULT_TIMEOUT

        self.filter_small = str(os.environ.get("LOGIC_INDEX_FILTER_SMALL", DEFAULT_FILTER_SMALL)).lower() == "true"
        remy_lang = os.environ.get("REMY_LANG", "en")
        self.lang = {"zh-CN": "Simplified Chinese", "en": "English"}.get(remy_lang, DEFAULT_LANG)

        self.exclusions = []
        self.layers = []
        self._load_config()
        self.cache = self._load_cache()
        self.dirty_nodes = []

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

    def _match_file_to_layer(self, rel_path):
        """Match a file path to a layer by directory segment patterns. Returns layer name or 'Core'."""
        segments = rel_path.replace("\\", "/").lower().split("/")
        for layer_def in self.layers:
            for segment in segments:
                for pattern in layer_def["patterns"]:
                    if segment == pattern or segment == pattern + "s":
                        return layer_def["name"]
        return "Core"

    def _load_cache(self):
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    cache_version = data.get("_meta", {}).get("version", "1.4.0")
                    if cache_version != VERSION:
                        print(f"检测到缓存版本升级 ({cache_version} -> {VERSION})。正在重置缓存...")
                        return {}
                    return data
            except Exception:
                pass
        return {}

    def _save_cache(self):
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        self.cache["_meta"] = {
            "last_updated": datetime.now().isoformat(),
            "model": self.model,
            "version": VERSION
        }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    def _calculate_hash(self, source_code, extra_data=""):
        normalized = "".join(source_code.split()) + extra_data
        return hashlib.md5(normalized.encode('utf-8')).hexdigest()

    def _call_llm(self, prompt):
        if not self.api_key:
            return "Error: OPENAI_API_KEY not set."

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
                    wait = (2 ** retries) + (random.random() * 0.3)
                    time.sleep(wait)
                    continue
                return f"Error: HTTP {e.code} - {e.reason}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if retries < self.retry_limit:
                    retries += 1
                    wait = (2 ** retries) + (random.random() * 0.3)
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

    def parse_file(self, file_path, parser):
        """Parses a source file using the given language parser."""
        rel_path = os.path.relpath(file_path, self.root_dir).replace(os.sep, '/')

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
        except Exception as e:
            print(f"Skipping {rel_path}: {e}")
            return None

        imports = parser.resolve_imports(source, file_path, self.root_dir)
        used_names = parser.collect_used_names(source)
        is_complex = any(ind in source for ind in parser.get_complexity_indicators())

        dep_summaries = []
        for imp_path, has_alias in imports.items():
            cached_imp = self.cache.get(imp_path)
            if cached_imp:
                for sym in cached_imp.get("symbols", []):
                    if sym.get("summary"):
                        is_used = sym['name'] in used_names
                        if not is_used and "." in sym['name']:
                            short_name = sym['name'].split(".")[-1]
                            is_used = short_name in used_names
                        if is_complex or has_alias or is_used:
                            dep_summaries.append(f"{sym['name']}:{sym['summary']}")

        extra_data = "|".join(sorted(dep_summaries))
        file_hash = self._calculate_hash(source, extra_data)

        import_list = list(imports.keys())
        file_node = {
            "path": rel_path,
            "hash": file_hash,
            "imports": import_list,
            "language": parser.__class__.__name__,
            "layer": self._match_file_to_layer(rel_path),
            "symbols": []
        }

        cached_file = self.cache.get(rel_path)
        file_changed = not cached_file or cached_file.get("hash") != file_hash

        symbols = parser.parse_symbols(source, file_path)
        for sym_info in symbols:
            self._process_symbol(sym_info, file_node, file_changed, cached_file, parser)

        call_edges = parser.extract_call_graph(source, file_path)
        file_node["calls"] = [{"caller": e.caller, "callee": e.callee, "line": e.line} for e in call_edges]

        return file_node

    def _process_symbol(self, sym_info, file_node, file_changed, cached_file, parser):
        """Process a single extracted symbol: check cache, extract docstring, queue for LLM."""
        symbol_hash = self._calculate_hash(sym_info.source_segment)

        summary = None
        if not file_changed and cached_file:
            for s in cached_file.get("symbols", []):
                if s["name"] == sym_info.name and s.get("hash") == symbol_hash:
                    summary = s.get("summary")
                    break

        if not summary:
            if sym_info.docstring:
                lines = [line.strip() for line in sym_info.docstring.splitlines() if line.strip()]
                if lines:
                    summary = "[Doc] " + " ".join(lines[:3])
            elif self.filter_small and len(sym_info.source_segment.splitlines()) < 3:
                summary = "Small utility function."

        symbol_data = {
            "name": sym_info.name,
            "args": sym_info.args,
            "type": sym_info.type,
            "lineno": sym_info.lineno,
            "hash": symbol_hash,
            "summary": summary
        }

        if not summary:
            self.dirty_nodes.append((file_node["path"], symbol_data, sym_info.source_segment, parser))

        file_node["symbols"].append(symbol_data)

    def _load_prompt_template(self, parser):
        """Loads the prompt template for the given parser's language."""
        try:
            prompt_path = parser.get_prompt_template_path()
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return "Task: Summarize source code: {source_code}"

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
                print(f"API Error for {file_path}: {res}")
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
            print(f"Error parsing batch response for {file_path}: {e}")

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
        except Exception:
            symbol['summary'] = "Error generating summary (Atomic fallback failed)"

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
            file_node = self.cache.get(fp)
            ctx_summary = ""
            if file_node and file_node.get("imports"):
                dep_list = []
                current_chars = 0
                for imp_path in file_node["imports"]:
                    cached_imp = self.cache.get(imp_path)
                    if cached_imp:
                        for s in cached_imp.get("symbols", []):
                            if s.get("summary"):
                                line = f"- {s['name']}: {s['summary']}"
                                if current_chars + len(line) + 1 > MAX_CTX_CHARS:
                                    print(f"Warning: Dependency context truncated for {fp} (Limit: {MAX_CTX_CHARS} chars)")
                                    break
                                dep_list.append(line)
                                current_chars += len(line) + 1
                        if current_chars > MAX_CTX_CHARS:
                            break
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
                        self.circuit_open = True
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    except Exception as e:
                        print(f"Error processing file batch: {e}")
            except KeyboardInterrupt:
                print("\nInterrupted by user. Shutting down...")
                executor.shutdown(wait=False, cancel_futures=True)
                raise

    def _resolve_call_edges(self):
        """Resolve callee names to qualified references using imports + cached symbols."""
        for path, file_data in self.cache.items():
            if path == "_meta" or "calls" not in file_data:
                continue
            calls = file_data["calls"]
            if not calls:
                continue

            symbol_map = {}
            for imp_path in file_data.get("imports", []):
                imp_data = self.cache.get(imp_path)
                if not imp_data:
                    continue
                for sym in imp_data.get("symbols", []):
                    name = sym["name"]
                    symbol_map[name] = f"{imp_path}::{name}"
                    if "." in name:
                        short = name.split(".")[-1]
                        symbol_map[short] = f"{imp_path}::{name}"

            for sym in file_data.get("symbols", []):
                name = sym["name"]
                if name not in symbol_map:
                    symbol_map[name] = f"{path}::{name}"
                if "." in name:
                    short = name.split(".")[-1]
                    if short not in symbol_map:
                        symbol_map[short] = f"{path}::{name}"

            for call in calls:
                qualified = symbol_map.get(call["callee"])
                if qualified:
                    call["callee_qualified"] = qualified

    def generate_markdown(self):
        git_hash = "Unknown"
        try:
            git_hash = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.root_dir,
                stderr=subprocess.DEVNULL
            ).decode('utf-8').strip()
        except Exception:
            pass

        lines = ["# 🧠 逻辑索引 (Logic Index)"]
        lines.append(f"> Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"> Git Commit: {git_hash}\n")
        lines.append("> **Symbol Types**: `[C]` Class | `[f]` Function | `[S]` Struct | `[E]` Enum | `[T]` Typedef/TypeAlias | `[M]` Macro | `[N]` Namespace | `[I]` Interface")
        lines.append("> **Tags**: `[Doc]` From Docstring/Doxygen | `[Source]` Data Source | `[Sink]` Data Sink | `[Util]` Utility | `[Test]` Test\n")

        layer_groups = {}
        for path, data in self.cache.items():
            if path == "_meta":
                continue
            if not data.get("symbols"):
                continue
            layer = self._match_file_to_layer(path)
            layer_groups.setdefault(layer, []).append((path, data))

        layer_order = [ld["name"] for ld in self.layers] + ["Core"]
        for layer_name in layer_order:
            if layer_name not in layer_groups:
                continue
            files = sorted(layer_groups[layer_name], key=lambda x: x[0])
            lines.append(f"## 🏗️ {layer_name}")
            for path, data in files:
                lines.append(f"### 📄 `{path}`")
                imports = data.get("imports", [])
                if imports:
                    lines.append(f"> Imports: {', '.join(imports)}")
                for sym in data["symbols"]:
                    icon = self._symbol_icon(sym["type"])
                    summary = sym.get("summary", "No summary")
                    name_display = f"{sym['name']}{sym.get('args', '')}"
                    lines.append(f"- **[{icon}]** `{name_display}`: {summary}")
                lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _symbol_icon(sym_type):
        icons = {
            "class": "C",
            "function": "f",
            "struct": "S",
            "enum": "E",
            "typedef": "T",
            "type_alias": "T",
            "macro": "M",
            "namespace": "N",
            "interface": "I",
        }
        return icons.get(sym_type, "?")

    def run(self):
        print("Scanning codebase...")

        try:
            new_cache = {}
            detected_languages = set()

            for root, dirs, files in os.walk(self.root_dir):
                dirs[:] = [d for d in dirs if not self._is_excluded(os.path.join(root, d))]

                for file in files:
                    self.stats["total_files"] += 1
                    full_path = os.path.join(root, file)

                    if self._is_excluded(full_path):
                        continue

                    parser = self._get_parser_for_file(file)
                    if not parser:
                        continue

                    lang_name = parser.__class__.__name__
                    detected_languages.add(lang_name)
                    self.stats["languages"][lang_name] = self.stats["languages"].get(lang_name, 0) + 1
                    self.stats["processed_files"] += 1

                    result = self.parse_file(full_path, parser)
                    if result:
                        new_cache[result["path"]] = result
                    else:
                        self.stats["failed_files"] += 1

            self.cache = new_cache
            self._resolve_call_edges()

            if self.dirty_nodes:
                if not self.api_key:
                    print("Warning: OPENAI_API_KEY not found. Skipping LLM generation.")
                else:
                    self.process_llm_queue()

        except Exception as e:
            if not isinstance(e, FatalError):
                print(f"Error during run: {e}")
        finally:
            self._save_cache()

            md_content = self.generate_markdown()
            os.makedirs(os.path.dirname(OUTPUT_MD), exist_ok=True)
            with open(OUTPUT_MD, 'w', encoding='utf-8') as f:
                f.write(md_content)

            print(f"\nLogic index updated at {OUTPUT_MD}")

            duration = time.time() - self.stats["start_time"]
            print("\n=== Logic Indexer Stats ===")
            print(f"Version             : {VERSION}")
            print(f"Total Files Scanned : {self.stats['total_files']}")
            print(f"Files Processed     : {self.stats['processed_files']}")
            lang_detail = ", ".join(f"{k}: {v}" for k, v in self.stats["languages"].items())
            if lang_detail:
                print(f"Languages           : {lang_detail}")
            print(f"Failed Files        : {self.stats['failed_files']}")
            print(f"API Calls           : {self.stats['api_calls']}")
            print(f"Total Duration      : {duration:.2f}s")
            print("===========================\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    indexer = LogicIndexer(os.getcwd())
    indexer.run()
