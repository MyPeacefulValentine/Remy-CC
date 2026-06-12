#!/usr/bin/env python3
"""Query implementations for the remy-index MCP server."""
import os
import sys
import sqlite3

_IMPACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "remy-index")
sys.path.insert(0, _IMPACT_DIR)
from impact import (
    bfs_callers as _bfs_callers,
    bfs_callees as _bfs_callees,
    collect_file_symbols,
    get_file_count,
    get_layer,
    get_line_range,
)

DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")
_DB_NOT_FOUND = "Error: logic_index.db not found. Run /remy-index to initialize the project index."


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_bool(name, default):
    val = os.environ.get(name, str(default)).lower()
    return val in ("true", "1", "yes")


MCP_BFS_MAX_DEPTH = _env_int("MCP_BFS_MAX_DEPTH", 5)
MCP_IMPACT_MAX_DEPTH_UP = _env_int("MCP_IMPACT_MAX_DEPTH_UP", 3)
MCP_IMPACT_MAX_DEPTH_DOWN = _env_int("MCP_IMPACT_MAX_DEPTH_DOWN", 3)
MCP_RESULT_LIMIT = _env_int("MCP_RESULT_LIMIT", 50)
MCP_STATIC_ONLY_DEFAULT = _env_bool("MCP_STATIC_ONLY_DEFAULT", False)


def _open_db():
    db_rel = os.environ.get("LOGIC_INDEX_DB_PATH", DB_FILE_DEFAULT)
    db_path = os.path.join(os.getcwd(), db_rel)
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(db_path, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=3000")
    return db


def _bfs_callers_ambiguous(db, target_set, max_depth, static_only=False):
    visited = set(target_set)
    current = set(target_set)
    levels = {}
    prov_filter = "AND e.provenance IS NULL" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        placeholders = ",".join(["?"] * len(current))
        sql = f"""
            SELECT DISTINCT source_file || '::' || caller FROM edges
            WHERE callee_qualified IN ({placeholders}) {prov_filter}
            UNION
            SELECT DISTINCT e.source_file || '::' || e.caller
            FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
            WHERE ec.candidate_qualified IN ({placeholders}) {prov_filter.replace('e.', 'e.')}
        """
        params = list(current) + list(current)
        rows = db.execute(sql, params).fetchall()
        next_level = {r[0] for r in rows} - visited
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        visited |= next_level
        current = next_level

    return levels


def _bfs_callees_ambiguous(db, target_set, max_depth, static_only=False):
    visited = set(target_set)
    current = set(target_set)
    levels = {}
    prov_filter = "AND e.provenance IS NULL" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        placeholders = ",".join(["?"] * len(current))
        sql = f"""
            SELECT DISTINCT callee_qualified FROM edges
            WHERE source_file || '::' || caller IN ({placeholders})
            AND callee_qualified IS NOT NULL {prov_filter}
            UNION
            SELECT DISTINCT ec.candidate_qualified
            FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
            WHERE e.source_file || '::' || e.caller IN ({placeholders}) {prov_filter}
        """
        params = list(current) + list(current)
        rows = db.execute(sql, params).fetchall()
        next_level = {r[0] for r in rows} - visited
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        visited |= next_level
        current = next_level

    return levels


def _resolve_symbol(db, name, file=None):
    if "::" in name:
        parts = name.split("::", 1)
        rows = db.execute(
            "SELECT file_path, name, type, args, lineno, end_lineno, summary "
            "FROM symbols WHERE file_path = ? AND name = ?",
            (parts[0], parts[1]),
        ).fetchall()
    elif file:
        rows = db.execute(
            "SELECT file_path, name, type, args, lineno, end_lineno, summary "
            "FROM symbols WHERE file_path = ? AND (name = ? OR short_name = ?)",
            (file, name, name),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT file_path, name, type, args, lineno, end_lineno, summary "
            "FROM symbols WHERE name = ? OR short_name = ?",
            (name, name),
        ).fetchall()
    return rows[:MCP_RESULT_LIMIT]


def query_symbol_impl(name, file=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        rows = _resolve_symbol(db, name, file)
        if not rows:
            return f"No symbols found matching '{name}'"
        lines = [f"symbols matching '{name}' ({len(rows)} results)\n"]
        for fpath, sname, stype, args, lineno, end_lineno, summary in rows:
            layer = get_layer(db, fpath)
            loc = f"L{lineno}" + (f"-L{end_lineno}" if end_lineno else "")
            sig = f"({args})" if args else ""
            lines.append(f"  [{stype}] {fpath}::{sname}{sig}  {fpath}:{loc} ({layer})")
            if summary:
                lines.append(f"        {summary}")
        return "\n".join(lines)
    finally:
        db.close()


def query_summary_impl(name, file=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        rows = _resolve_symbol(db, name, file)
        if not rows:
            return f"No symbols found matching '{name}'"
        lines = [f"summary for '{name}'\n"]
        for fpath, sname, stype, args, lineno, _end, summary in rows:
            sig = f"({args})" if args else ""
            lines.append(f"  [{stype}] {fpath}::{sname}{sig}  L{lineno}")
            lines.append(f"  summary: {summary or '(no summary available)'}")
            lines.append("")
        return "\n".join(lines)
    finally:
        db.close()


def query_callers_impl(symbol, depth, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth = min(depth, MCP_BFS_MAX_DEPTH)
        if static_only is None:
            static_only = MCP_STATIC_ONLY_DEFAULT

        targets = set()
        if "::" in symbol:
            targets.add(symbol)
        else:
            rows = db.execute(
                "SELECT file_path || '::' || name FROM symbols WHERE name = ? OR short_name = ?",
                (symbol, symbol),
            ).fetchall()
            targets = {r[0] for r in rows}

        if not targets:
            return f"No symbols found matching '{symbol}'"

        if include_ambiguous:
            levels = _bfs_callers_ambiguous(db, targets, depth, static_only)
        else:
            levels = _bfs_callers(db, targets, depth, static_only)

        return _format_bfs_result(db, f"callers of {symbol}", levels, depth)
    finally:
        db.close()


def query_callees_impl(symbol, depth, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth = min(depth, MCP_BFS_MAX_DEPTH)
        if static_only is None:
            static_only = MCP_STATIC_ONLY_DEFAULT

        targets = set()
        if "::" in symbol:
            targets.add(symbol)
        else:
            rows = db.execute(
                "SELECT file_path || '::' || name FROM symbols WHERE name = ? OR short_name = ?",
                (symbol, symbol),
            ).fetchall()
            targets = {r[0] for r in rows}

        if not targets:
            return f"No symbols found matching '{symbol}'"

        if include_ambiguous:
            levels = _bfs_callees_ambiguous(db, targets, depth, static_only)
        else:
            levels = _bfs_callees(db, targets, depth, static_only)

        return _format_bfs_result(db, f"callees of {symbol}", levels, depth)
    finally:
        db.close()


def query_impact_impl(files, depth_up, depth_down, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth_up = min(depth_up, MCP_BFS_MAX_DEPTH)
        depth_down = min(depth_down, MCP_BFS_MAX_DEPTH)
        if static_only is None:
            static_only = MCP_STATIC_ONLY_DEFAULT

        target_files = []
        for f in files:
            normalized = f.replace("\\", "/")
            row = db.execute("SELECT path FROM files WHERE path = ?", (normalized,)).fetchone()
            if row:
                target_files.append(row[0])

        if not target_files:
            return f"No indexed files found matching: {', '.join(files)}"

        seeds = set()
        for tf in target_files:
            seeds |= collect_file_symbols(db, tf)

        if not seeds:
            return f"No symbols found in: {', '.join(target_files)}"

        if include_ambiguous:
            upstream = _bfs_callers_ambiguous(db, seeds, depth_up, static_only) if depth_up > 0 else {}
            downstream = _bfs_callees_ambiguous(db, seeds, depth_down, static_only) if depth_down > 0 else {}
        else:
            upstream = _bfs_callers(db, seeds, depth_up, static_only) if depth_up > 0 else {}
            downstream = _bfs_callees(db, seeds, depth_down, static_only) if depth_down > 0 else {}

        return _format_impact_result(db, target_files, seeds, upstream, downstream)
    finally:
        db.close()


def query_patterns_impl(pattern_type=None, signal_name=None, file=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        conditions = []
        params = []
        if pattern_type:
            conditions.append("pattern_type = ?")
            params.append(pattern_type)
        if signal_name:
            conditions.append("signal_name = ?")
            params.append(signal_name)
        if file:
            conditions.append("file_path = ?")
            params.append(file)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT file_path, pattern_type, signal_name, handler, line FROM patterns WHERE {where} LIMIT ?"
        params.append(MCP_RESULT_LIMIT)

        rows = db.execute(sql, params).fetchall()
        if not rows:
            filters = []
            if pattern_type:
                filters.append(f"type={pattern_type}")
            if signal_name:
                filters.append(f"signal={signal_name}")
            if file:
                filters.append(f"file={file}")
            return f"No patterns found" + (f" ({', '.join(filters)})" if filters else "")

        lines = [f"event/callback patterns ({len(rows)} results)\n"]
        for fpath, ptype, signal, handler, line in rows:
            loc = f"L{line}" if line else ""
            lines.append(f"  [{ptype}] {signal or '?'} -> {handler or '?'}  {fpath}:{loc}")
        return "\n".join(lines)
    finally:
        db.close()


def _format_bfs_result(db, title, levels, max_depth):
    if not levels:
        return f"{title} ({max_depth} levels): no results"

    total = sum(len(v) for v in levels.values())
    lines = [f"{title} ({max_depth} levels, {total} results)\n"]

    for depth, qualified_list in sorted(levels.items()):
        lines.append(f"[depth {depth}]" + (" direct:" if depth == 1 else ""))
        count = 0
        for q in qualified_list:
            if count >= MCP_RESULT_LIMIT:
                lines.append(f"  ... ({len(qualified_list) - count} more)")
                break
            fpath = q.split("::")[0] if "::" in q else q
            layer = get_layer(db, fpath)
            lr = get_line_range(db, q)
            lines.append(f"  {q}{lr} ({layer})")
            count += 1
        lines.append("")

    return "\n".join(lines)


def _format_impact_result(db, target_files, seeds, upstream, downstream):
    all_files = set()
    lines = [f"impact analysis for: {', '.join(target_files)}\n"]

    lines.append("upstream (callers into these files):")
    if upstream:
        for depth, qualified_list in sorted(upstream.items()):
            entries = qualified_list[:MCP_RESULT_LIMIT]
            lines.append(f"  [depth {depth}] " + ", ".join(
                f"{q.split('::')[0]}" for q in entries[:5]
            ) + (f" ... +{len(entries)-5}" if len(entries) > 5 else ""))
            for q in entries:
                fpath = q.split("::")[0] if "::" in q else q
                all_files.add(fpath)
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("downstream (called by these files):")
    if downstream:
        for depth, qualified_list in sorted(downstream.items()):
            entries = qualified_list[:MCP_RESULT_LIMIT]
            lines.append(f"  [depth {depth}] " + ", ".join(
                f"{q.split('::')[0]}" for q in entries[:5]
            ) + (f" ... +{len(entries)-5}" if len(entries) > 5 else ""))
            for q in entries:
                fpath = q.split("::")[0] if "::" in q else q
                all_files.add(fpath)
    else:
        lines.append("  (none)")
    lines.append("")

    total_up = sum(len(v) for v in upstream.values())
    total_down = sum(len(v) for v in downstream.values())
    lines.append(f"summary: {len(all_files)} files affected, {total_up} upstream + {total_down} downstream symbols")

    return "\n".join(lines)
