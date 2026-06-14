#!/usr/bin/env python3
"""Query implementations for the remy-index MCP server."""
import difflib
import os
import sys
import sqlite3
from collections import defaultdict

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
from struct_scan import tokenize_symbol

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
FLOW_MAX_DEPTH = _env_int("FLOW_MAX_DEPTH", 15)
FLOW_MAX_VISITED = _env_int("FLOW_MAX_VISITED", 2000)


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
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ",".join(["?"] * len(chunk))
            sql = f"""
                SELECT DISTINCT source_file || '::' || caller FROM edges
                WHERE callee_qualified IN ({placeholders}) {prov_filter}
                UNION
                SELECT DISTINCT e.source_file || '::' || e.caller
                FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
                WHERE ec.candidate_qualified IN ({placeholders}) {prov_filter.replace('e.', 'e.')}
            """
            params = chunk + chunk
            all_rows.update(r[0] for r in db.execute(sql, params).fetchall())
        next_level = all_rows - visited
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
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ",".join(["?"] * len(chunk))
            sql = f"""
                SELECT DISTINCT callee_qualified FROM edges
                WHERE source_file || '::' || caller IN ({placeholders})
                AND callee_qualified IS NOT NULL {prov_filter}
                UNION
                SELECT DISTINCT ec.candidate_qualified
                FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
                WHERE e.source_file || '::' || e.caller IN ({placeholders}) {prov_filter}
            """
            params = chunk + chunk
            all_rows.update(r[0] for r in db.execute(sql, params).fetchall())
        next_level = all_rows - visited
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


def _fts_available(db):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols_fts'"
    ).fetchone()
    return row is not None


def _search_fts(db, text, limit, file_hint):
    tokens = tokenize_symbol(text)
    terms = tokens.split()
    if not terms:
        return []
    sanitized = [t.replace('"', '') for t in terms]
    sanitized = [t for t in sanitized if t]
    if not sanitized:
        return []
    fts_query = " ".join(f'"{t}"*' for t in sanitized)
    sql = (
        "SELECT s.name, s.file_path, s.lineno, s.type, s.short_name, "
        "bm25(symbols_fts, 10.0, 5.0, 1.0, 2.0) AS rank "
        "FROM symbols_fts "
        "JOIN symbols s ON s.id = symbols_fts.rowid "
        "WHERE symbols_fts MATCH ? "
    )
    params = [fts_query]
    if file_hint:
        sql += "AND s.file_path LIKE ? "
        params.append(f"%{file_hint}%")
    sql += "ORDER BY rank LIMIT ?"
    params.append(limit * 5)
    try:
        rows = db.execute(sql, params).fetchall()
    except Exception:
        return []
    results = []
    seen = set()
    for name, fpath, lineno, stype, short, rank in rows:
        key = (fpath, name)
        if key in seen:
            continue
        seen.add(key)
        bonus = 0
        if name.lower() == text.lower() or (short and short.lower() == text.lower()):
            bonus = -100
        results.append((name, fpath, lineno, stype, rank + bonus))
    results.sort(key=lambda r: r[4])
    return results[:limit]


def _search_like(db, text, limit, file_hint):
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    sql = (
        "SELECT name, file_path, lineno, type FROM symbols "
        "WHERE (name LIKE ? ESCAPE '\\' OR name_tokens LIKE ? ESCAPE '\\') "
    )
    pattern = f"%{escaped}%"
    params = [pattern, pattern]
    if file_hint:
        sql += "AND file_path LIKE ? "
        params.append(f"%{file_hint}%")
    sql += "LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    return [(name, fpath, lineno, stype, 0.0) for name, fpath, lineno, stype in rows]


def _search_fuzzy(db, text, limit, file_hint):
    sql = "SELECT DISTINCT name FROM symbols"
    params = []
    if file_hint:
        sql += " WHERE file_path LIKE ?"
        params.append(f"%{file_hint}%")
    all_names = [r[0] for r in db.execute(sql, params).fetchall()]
    cutoff = 0.6
    matches = difflib.get_close_matches(text, all_names, n=limit, cutoff=cutoff)
    if not matches:
        return []
    placeholders = ",".join(["?"] * len(matches))
    sql = f"SELECT name, file_path, lineno, type FROM symbols WHERE name IN ({placeholders})"
    if file_hint:
        sql += " AND file_path LIKE ?"
        matches_params = list(matches) + [f"%{file_hint}%"]
    else:
        matches_params = list(matches)
    rows = db.execute(sql, matches_params).fetchall()
    return [(name, fpath, lineno, stype, 0.0) for name, fpath, lineno, stype in rows]


def query_search_impl(text, limit=10, file_hint=""):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        if not _fts_available(db):
            return "FTS index not available. Run struct_scan to rebuild the index."

        results = _search_fts(db, text, limit, file_hint)
        search_level = "FTS5"

        if not results:
            results = _search_like(db, text, limit, file_hint)
            search_level = "LIKE"

        if not results:
            results = _search_fuzzy(db, text, limit, file_hint)
            search_level = "fuzzy"

        if not results:
            return f"No symbols found matching '{text}'"

        lines = [f"search results for '{text}' ({len(results)} results, matched via {search_level})\n"]
        for name, fpath, lineno, stype, score in results:
            layer = get_layer(db, fpath)
            loc = f"L{lineno}" if lineno else ""
            lines.append(f"  [{stype}] {fpath}::{name}  {fpath}:{loc} ({layer})")
        return "\n".join(lines)
    finally:
        db.close()


def _load_graph(db, static_only=False):
    prov_filter = "AND provenance IS NULL" if static_only else ""
    rows = db.execute(
        f"SELECT source_file, caller, callee_qualified, provenance, via "
        f"FROM edges WHERE callee_qualified IS NOT NULL {prov_filter}"
    ).fetchall()

    sym_rows = db.execute(
        "SELECT id, file_path || '::' || name, file_path, name, lineno, type "
        "FROM symbols"
    ).fetchall()

    name_to_id = {}
    id_to_info = {}
    for sid, qualified, fpath, sname, lineno, stype in sym_rows:
        name_to_id[qualified] = sid
        id_to_info[sid] = (qualified, fpath, sname, lineno, stype)

    adj_fwd = defaultdict(list)
    adj_bwd = defaultdict(list)
    skipped = 0
    for source_file, caller, callee_qualified, provenance, via in rows:
        src_q = f"{source_file}::{caller}"
        src_id = name_to_id.get(src_q)
        tgt_id = name_to_id.get(callee_qualified)
        if src_id is None or tgt_id is None:
            skipped += 1
            continue
        adj_fwd[src_id].append((tgt_id, provenance, via))
        adj_bwd[tgt_id].append((src_id, provenance, via))

    return adj_fwd, adj_bwd, name_to_id, id_to_info, skipped


def _bidir_bfs(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited):
    if src_id == tgt_id:
        return [(src_id, None, None)]

    fwd_parent = {src_id: (None, None, None)}
    bwd_parent = {tgt_id: (None, None, None)}
    front_f = [src_id]
    front_b = [tgt_id]

    for _depth in range(max_depth):
        if len(fwd_parent) + len(bwd_parent) > max_visited:
            return None
        if not front_f and not front_b:
            return None

        next_f = []
        for nid in front_f:
            for (t, prov, via) in adj_fwd.get(nid, []):
                if t not in fwd_parent:
                    fwd_parent[t] = (nid, prov, via)
                    next_f.append(t)
                if t in bwd_parent:
                    return _reconstruct_path(t, fwd_parent, bwd_parent, adj_fwd)
        front_f = next_f

        next_b = []
        for nid in front_b:
            for (s, prov, via) in adj_bwd.get(nid, []):
                if s not in bwd_parent:
                    bwd_parent[s] = (nid, prov, via)
                    next_b.append(s)
                if s in fwd_parent:
                    return _reconstruct_path(s, fwd_parent, bwd_parent, adj_fwd)
        front_b = next_b

    return None


def _reconstruct_path(meet, fwd_parent, bwd_parent, adj_fwd):
    fwd_half = []
    cur = meet
    while cur is not None:
        parent, prov, via = fwd_parent[cur]
        fwd_half.append((cur, prov, via))
        cur = parent
    fwd_half.reverse()

    bwd_half = []
    cur_bwd = bwd_parent[meet][0]
    while cur_bwd is not None:
        parent, _prov, _via = bwd_parent[cur_bwd]
        bwd_half.append(cur_bwd)
        cur_bwd = parent

    result = list(fwd_half)
    prev_id = meet
    for nid in bwd_half:
        edge_prov = None
        edge_via = None
        for (t, ep, ev) in adj_fwd.get(prev_id, []):
            if t == nid:
                edge_prov = ep
                edge_via = ev
                break
        result.append((nid, edge_prov, edge_via))
        prev_id = nid

    return result


def _resolve_flow_symbol(sym, db, name_to_id, adj_fwd, adj_bwd, resolved_ids, all_tokens):
    if "/" in sym and ":" in sym:
        idx = sym.rfind(":")
        file_part = sym[:idx]
        name_part = sym[idx + 1:]
        rows = db.execute(
            "SELECT file_path || '::' || name FROM symbols "
            "WHERE file_path LIKE ? AND (name = ? OR short_name = ?)",
            (f"%{file_part}%", name_part, name_part),
        ).fetchall()
        if rows:
            return name_to_id.get(rows[0][0]), rows[0][0], False
        return None, sym, False

    if "." in sym or "::" in sym:
        sep = "::" if "::" in sym else "."
        parts = sym.rsplit(sep, 1)
        class_hint = parts[0]
        method_name = parts[1]
        rows = db.execute(
            "SELECT file_path || '::' || name FROM symbols "
            "WHERE (name = ? OR short_name = ?)",
            (method_name, method_name),
        ).fetchall()
        candidates = [(q,) for (q,) in rows if class_hint.lower() in q.lower()]
        if candidates:
            return name_to_id.get(candidates[0][0]), candidates[0][0], False
        if rows:
            return name_to_id.get(rows[0][0]), rows[0][0], len(rows) > 1
        return None, sym, False

    rows = db.execute(
        "SELECT file_path || '::' || name FROM symbols "
        "WHERE name = ? OR short_name = ?",
        (sym, sym),
    ).fetchall()

    if not rows:
        return None, sym, False
    if len(rows) == 1:
        return name_to_id.get(rows[0][0]), rows[0][0], False

    candidates = [(q, name_to_id.get(q)) for (q,) in rows if name_to_id.get(q) is not None]
    if not candidates:
        return name_to_id.get(rows[0][0]), rows[0][0], True
    if len(candidates) == 1:
        return candidates[0][1], candidates[0][0], False

    if resolved_ids:
        for q, sid in candidates:
            reachable = set()
            frontier = [sid]
            for _ in range(2):
                nxt = []
                for n in frontier:
                    for (t, _, _) in adj_fwd.get(n, []):
                        if t not in reachable:
                            reachable.add(t)
                            nxt.append(t)
                    for (t, _, _) in adj_bwd.get(n, []):
                        if t not in reachable:
                            reachable.add(t)
                            nxt.append(t)
                frontier = nxt
            if reachable & resolved_ids:
                return sid, q, False

    other_tokens = {t.lower() for t in all_tokens if t.lower() != sym.lower()}
    if other_tokens:
        for q, sid in candidates:
            q_lower = q.lower()
            if any(tok in q_lower for tok in other_tokens):
                return sid, q, False

    degree_list = []
    for q, sid in candidates:
        deg = len(adj_fwd.get(sid, [])) + len(adj_bwd.get(sid, []))
        degree_list.append((deg, len(q), sid, q))
    degree_list.sort(key=lambda x: (-x[0], x[1]))
    chosen = degree_list[0]
    return chosen[2], chosen[3], len(degree_list) > 1


def _format_flow(resolved, segments, id_to_info, static_only, max_depth):
    total_connected = sum(1 for s in segments if s is not None)
    if total_connected == 0:
        return "No connected paths found among the queried symbols."

    partial = total_connected < len(segments)
    if partial:
        header = f"## Flow (partial — {total_connected + 1}/{len(resolved)} symbols connected)\n"
    else:
        header = "## Flow (call path among queried symbols)\n"
    lines = [header]
    step = 1

    for i, (sym_id, sym_qualified, ambiguous) in enumerate(resolved):
        if sym_id is None:
            lines.append(f"\n[Unresolved: '{sym_qualified}' not found in index]\n")
            continue

        if i > 0 and segments[i - 1] is None:
            prev_name = resolved[i - 1][1].split("::")[-1] if "::" in resolved[i - 1][1] else resolved[i - 1][1]
            cur_name = sym_qualified.split("::")[-1] if "::" in sym_qualified else sym_qualified
            lines.append(f"\n[Break: pair ({prev_name}, {cur_name}) not connected within depth={max_depth}]")
            if static_only:
                lines.append("[Note: static_only=True excludes synthesized paths]")
            lines.append("")

        if i == 0 or segments[i - 1] is None:
            info = id_to_info.get(sym_id)
            if info:
                _, fpath, sname, lineno, _ = info
                loc = f":{lineno}" if lineno else ""
                amb_note = " [ambiguous: resolved by edge_count]" if ambiguous else ""
                lines.append(f"{step}. {sname} ({fpath}{loc}){amb_note}")
                step += 1

        if i < len(segments) and segments[i] is not None:
            path = segments[i]
            for j in range(1, len(path)):
                nid, prov, via = path[j]
                if prov == "heuristic":
                    edge_label = f"synthesized [via: {via}]" if via else "synthesized"
                elif prov == "ambiguous":
                    edge_label = "call [ambiguous resolution]"
                else:
                    edge_label = "call"
                lines.append(f"   ↓ {edge_label}")
                info = id_to_info.get(nid)
                if info:
                    _, fpath, sname, lineno, _ = info
                    loc = f":{lineno}" if lineno else ""
                    amb_note = ""
                    if j == len(path) - 1 and i + 1 < len(resolved) and resolved[i + 1][2]:
                        amb_note = " [ambiguous: resolved by edge_count]"
                    lines.append(f"{step}. {sname} ({fpath}{loc}){amb_note}")
                    step += 1

    return "\n".join(lines)


def query_flow_impl(symbols, max_depth=None, max_visited=None, static_only=False):
    if not symbols or len(symbols) < 2:
        return "Error: query_flow requires at least 2 symbols."

    if max_depth is None:
        max_depth = FLOW_MAX_DEPTH
    if max_visited is None:
        max_visited = FLOW_MAX_VISITED
    if static_only is None:
        static_only = MCP_STATIC_ONLY_DEFAULT

    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        adj_fwd, adj_bwd, name_to_id, id_to_info, _skipped = _load_graph(db, static_only)
        if not adj_fwd and not adj_bwd:
            return "No edges in index. Run struct_scan to build the call graph."

        all_tokens = []
        for sym in symbols:
            if "/" in sym and ":" in sym:
                all_tokens.append(sym.rsplit(":", 1)[-1])
            elif "." in sym:
                all_tokens.extend(sym.split("."))
            elif "::" in sym:
                all_tokens.extend(sym.split("::"))
            else:
                all_tokens.append(sym)

        resolved = []
        resolved_ids = set()
        for sym in symbols:
            sid, qualified, ambiguous = _resolve_flow_symbol(
                sym, db, name_to_id, adj_fwd, adj_bwd, resolved_ids, all_tokens
            )
            resolved.append((sid, qualified, ambiguous))
            if sid is not None:
                resolved_ids.add(sid)

        unresolved = [r[1] for r in resolved if r[0] is None]
        if len(unresolved) == len(resolved):
            return f"No symbols resolved: {', '.join(unresolved)}"

        segments = []
        for i in range(len(resolved) - 1):
            src_id = resolved[i][0]
            tgt_id = resolved[i + 1][0]
            if src_id is None or tgt_id is None:
                segments.append(None)
                continue
            path = _bidir_bfs(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited)
            segments.append(path)

        return _format_flow(resolved, segments, id_to_info, static_only, max_depth)
    finally:
        db.close()
