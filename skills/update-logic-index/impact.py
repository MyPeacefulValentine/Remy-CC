#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Impact radius analysis via BFS on logic_index.json call graph data."""

import json
import os
import sys

CACHE_FILE = os.path.join(".claude", "logic_index.json")

DEFAULT_DEPTH_UP = 2
DEFAULT_DEPTH_DOWN = 2


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


def build_forward_index(cache):
    """Map caller_qualified -> list of (callee_file, callee_func)."""
    fwd = {}
    for path, data in cache.items():
        if path == "_meta":
            continue
        for call in data.get("calls", []):
            qualified = call.get("callee_qualified")
            if qualified:
                caller_q = f"{path}::{call['caller']}"
                callee_file = qualified.split("::")[0]
                callee_func = qualified.split("::")[-1]
                fwd.setdefault(caller_q, []).append((callee_file, callee_func))
    return fwd


def collect_file_symbols(cache, file_path):
    """Return set of qualified names for all symbols in a file."""
    data = cache.get(file_path)
    if not data:
        return set()
    return {f"{file_path}::{s['name']}" for s in data.get("symbols", [])}


def bfs(cache, adjacency_index, target_files, max_depth):
    seeds = set()
    for f in target_files:
        seeds |= collect_file_symbols(cache, f)

    visited = set(seeds)
    levels = {}

    current = seeds
    for depth in range(1, max_depth + 1):
        next_level = set()
        for qualified in current:
            for neighbor_file, neighbor_func in adjacency_index.get(qualified, []):
                neighbor_q = f"{neighbor_file}::{neighbor_func}"
                if neighbor_q not in visited:
                    next_level.add(neighbor_q)
                    visited.add(neighbor_q)
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


def format_output(cache, seeds, upstream_levels, downstream_levels, target_files):
    lines = []
    all_layers = set()
    all_files = set(target_files)

    lines.append("[Modified]")
    for q in sorted(seeds):
        fpath = q.split("::")[0]
        layer = get_layer(cache, fpath)
        all_layers.add(layer)
        all_files.add(fpath)
        lines.append(f"  {q} ({layer})")
    lines.append("")

    if upstream_levels:
        for depth, qualified_list in sorted(upstream_levels.items()):
            lines.append(f"[Upstream Depth {depth}]")
            for q in qualified_list:
                fpath = q.split("::")[0]
                layer = get_layer(cache, fpath)
                all_layers.add(layer)
                all_files.add(fpath)
                lines.append(f"  {q} ({layer})")
            lines.append("")

    if downstream_levels:
        for depth, qualified_list in sorted(downstream_levels.items()):
            lines.append(f"[Downstream Depth {depth}]")
            for q in qualified_list:
                fpath = q.split("::")[0]
                layer = get_layer(cache, fpath)
                all_layers.add(layer)
                all_files.add(fpath)
                lines.append(f"  {q} ({layer})")
            lines.append("")

    total_funcs = len(seeds)
    for levels in (upstream_levels, downstream_levels):
        total_funcs += sum(len(v) for v in levels.items())

    lines.append(f"Summary: {len(all_files)} files, {total_funcs} functions, {len(all_layers)} layers")

    if len(all_layers) >= 3:
        lines.append(f"⚠ Cross-layer impact: {', '.join(sorted(all_layers))}")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Impact radius analysis via BFS on call graph")
    parser.add_argument("files", nargs="+", help="Target files (relative paths, forward slashes)")
    parser.add_argument("--depth", type=int, default=None, help="Max BFS depth for both directions")
    parser.add_argument("--depth-up", type=int, default=None, help="Max upstream (callers) BFS depth")
    parser.add_argument("--depth-down", type=int, default=None, help="Max downstream (callees) BFS depth")
    parser.add_argument("--direction", choices=["reverse", "forward", "both"], default="both",
                        help="BFS direction (default: both)")
    parser.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    args = parser.parse_args()

    env_depth_up = DEFAULT_DEPTH_UP
    env_depth_down = DEFAULT_DEPTH_DOWN
    try:
        env_depth_up = int(os.environ.get("IMPACT_DEPTH_UP", DEFAULT_DEPTH_UP))
    except ValueError:
        pass
    try:
        env_depth_down = int(os.environ.get("IMPACT_DEPTH_DOWN", DEFAULT_DEPTH_DOWN))
    except ValueError:
        pass

    base_depth_up = args.depth if args.depth is not None else env_depth_up
    base_depth_down = args.depth if args.depth is not None else env_depth_down
    depth_up = args.depth_up if args.depth_up is not None else base_depth_up
    depth_down = args.depth_down if args.depth_down is not None else base_depth_down

    cache = load_cache(args.cwd)

    has_calls = any(
        data.get("calls") for path, data in cache.items() if path != "_meta"
    )
    if not has_calls:
        print("Warning: No call graph data found in logic_index.json", file=sys.stderr)
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

    seeds = set()
    for f in target_files:
        seeds |= collect_file_symbols(cache, f)

    upstream_levels = {}
    downstream_levels = {}

    if args.direction in ("reverse", "both") and depth_up > 0:
        reverse_index = build_reverse_index(cache)
        upstream_levels = bfs(cache, reverse_index, target_files, depth_up)

    if args.direction in ("forward", "both") and depth_down > 0:
        forward_index = build_forward_index(cache)
        downstream_levels = bfs(cache, forward_index, target_files, depth_down)

    print(format_output(cache, seeds, upstream_levels, downstream_levels, target_files))


if __name__ == "__main__":
    main()
