#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Impact radius analysis via BFS on call graph (SQLite backend).
"""

import json
import os
import sys
import sqlite3

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _auto_depth(file_count):
    if file_count <= 50:
        return 3, 3
    elif file_count <= 200:
        return 2, 2
    else:
        return 2, 1


def open_db(cwd):
    db_path = str(remy_config.load_config(cwd, strict=True).get("REMY_LOGIC_INDEX_DB_PATH"))
    if not os.path.exists(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        sys.exit(2)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_file_count(db):
    row = db.execute("SELECT COUNT(*) FROM files").fetchone()
    return row[0] if row else 0


def collect_file_symbols(db, file_path):
    rows = db.execute(
        "SELECT file_path || '::' || name FROM symbols WHERE file_path = ?",
        (file_path,)
    ).fetchall()
    return {r[0] for r in rows}


def bfs_callers(db, target_qualified_set, max_depth, static_only=False):
    visited = set(target_qualified_set)
    current = set(target_qualified_set)
    levels = {}
    filter_clause = "AND provenance IN ('definite','probable')" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ','.join(['?'] * len(chunk))
            sql = f"""
                SELECT DISTINCT source_file || '::' || caller
                FROM edges
                WHERE callee_qualified IN ({placeholders})
                {filter_clause}
            """
            all_rows.update(r[0] for r in db.execute(sql, chunk).fetchall())
        next_level = all_rows - visited
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        visited |= next_level
        current = next_level

    return levels


def bfs_callees(db, target_qualified_set, max_depth, static_only=False):
    visited = set(target_qualified_set)
    current = set(target_qualified_set)
    levels = {}
    filter_clause = "AND provenance IN ('definite','probable')" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ','.join(['?'] * len(chunk))
            sql = f"""
                SELECT DISTINCT callee_qualified
                FROM edges
                WHERE source_file || '::' || caller IN ({placeholders})
                AND callee_qualified IS NOT NULL
                {filter_clause}
            """
            all_rows.update(r[0] for r in db.execute(sql, chunk).fetchall())
        next_level = all_rows - visited
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        visited |= next_level
        current = next_level

    return levels


def get_layer(db, file_path):
    row = db.execute("SELECT layer FROM files WHERE path = ?", (file_path,)).fetchone()
    return row[0] if row else "Unknown"


def get_line_range(db, qualified):
    if "::" not in qualified:
        return ""
    fpath, name = qualified.split("::", 1)
    row = db.execute(
        "SELECT lineno, end_lineno FROM symbols WHERE file_path = ? AND name = ?",
        (fpath, name)
    ).fetchone()
    if row:
        start, end = row
        if start and end:
            return f" [L{start}-L{end}]"
        elif start:
            return f" [L{start}]"
    return ""


def format_output(db, seeds, upstream_levels, downstream_levels, target_files):
    lines = ["[Modified]"]
    all_layers = set()
    all_files = set()

    for q in sorted(seeds):
        fpath = q.split("::")[0] if "::" in q else q
        layer = get_layer(db, fpath)
        all_layers.add(layer)
        all_files.add(fpath)
        lines.append(f"  {q}{get_line_range(db, q)} ({layer})")
    lines.append("")

    if upstream_levels:
        for depth, qualified_list in sorted(upstream_levels.items()):
            lines.append(f"[Upstream Depth {depth}]")
            for q in qualified_list:
                fpath = q.split("::")[0] if "::" in q else q
                layer = get_layer(db, fpath)
                all_layers.add(layer)
                all_files.add(fpath)
                lines.append(f"  {q}{get_line_range(db, q)} ({layer})")
            lines.append("")

    if downstream_levels:
        for depth, qualified_list in sorted(downstream_levels.items()):
            lines.append(f"[Downstream Depth {depth}]")
            for q in qualified_list:
                fpath = q.split("::")[0] if "::" in q else q
                layer = get_layer(db, fpath)
                all_layers.add(layer)
                all_files.add(fpath)
                lines.append(f"  {q}{get_line_range(db, q)} ({layer})")
            lines.append("")

    total_funcs = len(seeds)
    for levels in (upstream_levels, downstream_levels):
        total_funcs += sum(len(v) for v in levels.values())

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
    parser.add_argument("--static-only", action="store_true",
                        help="Exclude heuristic (synthesized) edges from BFS")
    parser.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    args = parser.parse_args()

    db = open_db(args.cwd)
    file_count = get_file_count(db)
    auto_up, auto_down = _auto_depth(file_count)

    depth_up = args.depth or args.depth_up or auto_up
    depth_down = args.depth or args.depth_down or auto_down

    target_files = []
    for f in args.files:
        normalized = f.replace("\\", "/")
        row = db.execute("SELECT path FROM files WHERE path = ?", (normalized,)).fetchone()
        if row:
            target_files.append(row[0])
        else:
            print(f"Warning: {normalized} not found in logic_index.db", file=sys.stderr)

    if not target_files:
        print("[Modified]\n\nSummary: 0 files, 0 functions, 0 layers")
        db.close()
        return

    seeds = set()
    for tf in target_files:
        seeds |= collect_file_symbols(db, tf)

    upstream_levels = {}
    downstream_levels = {}

    if args.direction in ("reverse", "both"):
        upstream_levels = bfs_callers(db, seeds, depth_up, args.static_only)

    if args.direction in ("forward", "both"):
        downstream_levels = bfs_callees(db, seeds, depth_down, args.static_only)

    output = format_output(db, seeds, upstream_levels, downstream_levels, target_files)
    print(output)
    db.close()


if __name__ == "__main__":
    main()
