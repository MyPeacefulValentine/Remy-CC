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

from parsers.python_parser import PythonParser
from parsers.c_cpp_parser import CCppParser
from parsers.ts_parser import TSParser
from struct_scan import StructScanner

VERSION = "3.0.0"
CACHE_FILE = os.path.join(".claude", "logic_index.json")
DIRTY_FILE = os.path.join(".claude", "logic_index_dirty")
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

    def _load_cache(self):
        cache_path = os.path.join(self.root_dir, CACHE_FILE)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
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
        cache_path = os.path.join(self.root_dir, CACHE_FILE)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        self.cache["_meta"] = {
            "last_updated": datetime.now().isoformat(),
            "model": self.model,
            "version": VERSION
        }
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

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
            if self._is_path_excluded(path):
                continue
            layer = data.get("layer", "Core")
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
            scanner = StructScanner(self.root_dir)
            scanner.scan_all()
            self.cache = scanner.cache

            dirty_by_file = {}
            for path, file_data in self.cache.items():
                if path == "_meta":
                    continue
                for sym in file_data.get("symbols", []):
                    if not sym.get("summary"):
                        dirty_by_file.setdefault(path, []).append(sym)

            for path, syms in dirty_by_file.items():
                full_path = os.path.join(self.root_dir, path)
                parser = self._get_parser_for_file(os.path.basename(path))
                if not parser:
                    continue
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        source = f.read()
                except Exception:
                    continue
                parsed = parser.parse_symbols(source, full_path)
                seg_map = {s.name: s.source_segment for s in parsed}
                for sym in syms:
                    segment = seg_map.get(sym["name"], "")
                    if segment:
                        self.dirty_nodes.append((path, sym, segment, parser))

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
            md_path = os.path.join(self.root_dir, OUTPUT_MD)
            os.makedirs(os.path.dirname(md_path), exist_ok=True)
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(md_content)

            print(f"\nLogic index updated at {OUTPUT_MD}")

            dirty_path = os.path.join(self.root_dir, DIRTY_FILE)
            try:
                if os.path.exists(dirty_path):
                    os.remove(dirty_path)
            except OSError:
                pass

            duration = time.time() - self.stats["start_time"]
            dirty_count = len(self.dirty_nodes)
            file_count = sum(1 for k in self.cache if k != "_meta")
            print("\n=== Logic Indexer Stats ===")
            print(f"Version             : {VERSION}")
            print(f"Files in Index      : {file_count}")
            print(f"Symbols for LLM     : {dirty_count}")
            print(f"API Calls           : {self.stats['api_calls']}")
            print(f"Total Duration      : {duration:.2f}s")
            print("===========================\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    indexer = LogicIndexer(os.getcwd())
    indexer.run()
