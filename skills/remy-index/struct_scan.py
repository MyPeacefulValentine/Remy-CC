#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Structural scanner for logic_index.json.
Extracts symbols, call graphs, imports, and line ranges without LLM dependency.
Designed for hook-driven incremental and full scans.
"""

import hashlib
import json
import os
import re
import sys
import fnmatch
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers.base import SymbolInfo
from parsers.python_parser import PythonParser
from parsers.c_cpp_parser import CCppParser
from parsers.ts_parser import TSParser

VERSION = "3.0.0"
CACHE_FILE = os.path.join(".claude", "logic_index.json")
CONFIG_FILE = os.path.join(".claude", "logic_index_config")


class StructScanner:
    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)
        self.exclusions = []
        self.layers = []
        self._load_config()
        self.cache = self._load_cache()
        self.old_cache = {}

        self.filter_small = str(os.environ.get("LOGIC_INDEX_FILTER_SMALL", "false")).lower() == "true"
        self.parsers = [PythonParser(), CCppParser(), TSParser()]
        self._extension_map = {}
        for parser in self.parsers:
            for ext in parser.get_extensions():
                self._extension_map[ext] = parser

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

    def _load_cache(self):
        cache_path = os.path.join(self.root_dir, CACHE_FILE)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    cache_version = data.get("_meta", {}).get("version", "1.4.0")
                    if cache_version != VERSION:
                        return {}
                    return data
            except Exception:
                pass
        return {}

    def _save_cache(self):
        cache_path = os.path.join(self.root_dir, CACHE_FILE)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        meta = self.cache.get("_meta", {})
        meta["last_updated"] = datetime.now().isoformat()
        meta["version"] = VERSION
        self.cache["_meta"] = meta
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

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
        rel_path = os.path.relpath(file_path, self.root_dir).replace(os.sep, '/')

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
        except Exception:
            return None

        struct_hash = self._compute_struct_hash(source)
        cached_file = self.old_cache.get(rel_path)

        if cached_file and cached_file.get("struct_hash") == struct_hash:
            return cached_file

        imports = parser.resolve_imports(source, file_path, self.root_dir)
        symbols = parser.parse_symbols(source, file_path)
        call_edges = parser.extract_call_graph(source, file_path)

        file_node = {
            "path": rel_path,
            "struct_hash": struct_hash,
            "imports": list(imports.keys()),
            "language": parser.__class__.__name__,
            "layer": self._match_file_to_layer(rel_path),
            "symbols": [],
            "calls": [{"caller": e.caller, "callee": e.callee, "line": e.line} for e in call_edges],
        }

        if cached_file and "hash" in cached_file:
            file_node["hash"] = cached_file["hash"]

        for sym_info in symbols:
            stripped = self._strip_comments(sym_info.source_segment, parser)
            symbol_hash = self._calculate_symbol_hash(stripped)

            summary = None
            if cached_file:
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

            file_node["symbols"].append({
                "name": sym_info.name,
                "args": sym_info.args,
                "type": sym_info.type,
                "lineno": sym_info.lineno,
                "end_lineno": sym_info.end_lineno,
                "hash": symbol_hash,
                "summary": summary,
            })

        return file_node

    def _resolve_call_edges(self):
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

    def scan_all(self):
        self.old_cache = dict(self.cache)
        new_cache = {}

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
                    new_cache[result["path"]] = result

        for path, data in self.old_cache.items():
            if path == "_meta":
                continue
            if path not in new_cache and self._is_path_excluded(path):
                new_cache[path] = data

        self.cache = new_cache
        self._resolve_call_edges()
        self._save_cache()

    def scan_files(self, file_paths):
        self.old_cache = dict(self.cache)
        for file_path in file_paths:
            if os.path.isabs(file_path):
                full_path = file_path
            else:
                full_path = os.path.join(self.root_dir, file_path)

            if not os.path.exists(full_path):
                continue

            parser = self._get_parser_for_file(os.path.basename(full_path))
            if not parser:
                continue

            result = self.scan_file(full_path, parser)
            if result:
                self.cache[result["path"]] = result

        self._resolve_call_edges()
        self._save_cache()


def scan_all(root_dir):
    scanner = StructScanner(root_dir)
    scanner.scan_all()
    return scanner.cache


def scan_files(root_dir, file_paths):
    scanner = StructScanner(root_dir)
    scanner.scan_files(file_paths)
    return scanner.cache


if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description="Structural scan for logic_index.json")
    ap.add_argument("--files", nargs="*", help="Incremental: only scan these files")
    ap.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    args = ap.parse_args()

    if args.files:
        scan_files(args.cwd, args.files)
    else:
        scan_all(args.cwd)
