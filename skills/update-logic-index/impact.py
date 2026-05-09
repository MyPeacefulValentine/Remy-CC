#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Impact radius analysis via BFS on logic_index.json call graph data."""

import json
import os
import sys

CACHE_FILE = os.path.join(".claude", "logic_index.json")


def load_cache(cwd):
    path = os.path.join(cwd, CACHE_FILE)
    if not os.path.exists(path):
        print(f"Error: {CACHE_FILE} not found in {cwd}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_reverse_index(cache):
    """Map callee_qualified -> list of (caller_file, caller_func)."""
    rev = {}
    for path, data in cache.items():
        if path == "_meta":
            continue
        for call in data.get("calls", []):
            qualified = call.get("callee_qualified")
            if qualified:
                rev.setdefault(qualified, []).append((path, call["caller"]))
    return rev


def collect_file_symbols(cache, file_path):
    """Return set of qualified names for all symbols in a file."""
    data = cache.get(file_path)
    if not data:
        return set()
    return {f"{file_path}::{s['name']}" for s in data.get("symbols", [])}


def bfs(cache, reverse_index, target_files, max_depth):
    seeds = set()
    for f in target_files:
        seeds |= collect_file_symbols(cache, f)

    visited = set(seeds)
    levels = {0: sorted(seeds)}

    current = seeds
    for depth in range(1, max_depth + 1):
        next_level = set()
        for qualified in current:
            for caller_file, caller_func in reverse_index.get(qualified, []):
                caller_q = f"{caller_file}::{caller_func}"
                if caller_q not in visited:
                    next_level.add(caller_q)
                    visited.add(caller_q)
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        current = next_level

    return levels


def get_layer(cache, file_path):
    data = cache.get(file_path)
    if data:
        return data.get("layer", "Core")
    return "Unknown"


def format_output(cache, levels, target_files):
    lines = []
    all_layers = set()
    all_files = set(target_files)

    for depth, qualified_list in sorted(levels.items()):
        tag = "Modified" if depth == 0 else f"Depth {depth}"
        lines.append(f"[{tag}]")
        for q in qualified_list:
            fpath = q.split("::")[0]
            layer = get_layer(cache, fpath)
            all_layers.add(layer)
            all_files.add(fpath)
            lines.append(f"  {q} ({layer})")
        lines.append("")

    total_funcs = sum(len(v) for v in levels.values())
    lines.append(f"Summary: {len(all_files)} files, {total_funcs} functions, {len(all_layers)} layers")

    if len(all_layers) >= 3:
        lines.append(f"⚠ Cross-layer impact: {', '.join(sorted(all_layers))}")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Impact radius analysis via BFS on call graph")
    parser.add_argument("files", nargs="+", help="Target files (relative paths, forward slashes)")
    parser.add_argument("--depth", type=int, default=2, help="Max BFS depth (default: 2)")
    parser.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    args = parser.parse_args()

    cache = load_cache(args.cwd)

    has_calls = any(
        data.get("calls") for path, data in cache.items() if path != "_meta"
    )
    if not has_calls:
        print("Warning: No call graph data found in logic_index.json (regex-only parsers produce no CALLS data)", file=sys.stderr)
        sys.exit(2)

    target_files = []
    for f in args.files:
        if os.path.isabs(f):
            try:
                f = os.path.relpath(f, args.cwd)
            except ValueError:
                print(f"Warning: cannot relativize {f} against {args.cwd}", file=sys.stderr)
                continue
        target_files.append(f.replace(os.sep, "/"))
    missing = [f for f in target_files if f not in cache]
    if missing:
        for m in missing:
            print(f"Warning: {m} not found in logic_index.json", file=sys.stderr)

    reverse_index = build_reverse_index(cache)
    levels = bfs(cache, reverse_index, target_files, args.depth)
    print(format_output(cache, levels, target_files))


if __name__ == "__main__":
    main()
