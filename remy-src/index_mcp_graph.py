#!/usr/bin/env python3
"""Graph queries for the remy-index MCP server: BFS, impact, and flow."""
from collections import defaultdict

from index_mcp_common import (
    _DB_NOT_FOUND,
    _config_values,
    _open_db,
    _query_scoped,
)
from impact import (
    bfs_callers as _bfs_callers,
    bfs_callees as _bfs_callees,
    collect_file_symbols,
    get_layer,
    get_line_range,
)
from schema import STATIC_PROVENANCE_SQL

_IMPACT_LABELS_PER_LEVEL = 5


def _bfs_callers_ambiguous(db, target_set, max_depth, static_only=False):
    visited = set(target_set)
    current = set(target_set)
    levels = {}
    prov_filter = f"AND e.provenance {STATIC_PROVENANCE_SQL}" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ",".join(["?"] * len(chunk))
            sql = f"""
                SELECT DISTINCT source_file || '::' || caller FROM edges e
                WHERE callee_qualified IN ({placeholders}) {prov_filter}
                UNION
                SELECT DISTINCT e.source_file || '::' || e.caller
                FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
                WHERE ec.candidate_qualified IN ({placeholders}) {prov_filter}
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
    prov_filter = f"AND e.provenance {STATIC_PROVENANCE_SQL}" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ",".join(["?"] * len(chunk))
            sql = f"""
                SELECT DISTINCT callee_qualified FROM edges e
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


@_query_scoped
def query_callers_impl(symbol, depth, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth = min(depth, _config_values()[0])
        if static_only is None:
            static_only = _config_values()[2]

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


@_query_scoped
def query_callees_impl(symbol, depth, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth = min(depth, _config_values()[0])
        if static_only is None:
            static_only = _config_values()[2]

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


@_query_scoped
def query_impact_impl(files, depth_up, depth_down, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth_up = min(depth_up, _config_values()[0])
        depth_down = min(depth_down, _config_values()[0])
        if static_only is None:
            static_only = _config_values()[2]

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

        return _format_impact_result(target_files, upstream, downstream)
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
            if count >= _config_values()[1]:
                lines.append(f"  ... ({len(qualified_list) - count} more)")
                break
            fpath = q.split("::")[0] if "::" in q else q
            layer = get_layer(db, fpath)
            lr = get_line_range(db, q)
            lines.append(f"  {q}{lr} ({layer})")
            count += 1
        lines.append("")

    return "\n".join(lines)


def _impact_level_files(qualified_list):
    """Distinct file paths of one BFS level, in first-seen order."""
    files = []
    seen = set()
    for qualified in qualified_list:
        fpath = qualified.split("::")[0] if "::" in qualified else qualified
        if fpath not in seen:
            seen.add(fpath)
            files.append(fpath)
    return files


def _format_impact_result(target_files, upstream, downstream):
    all_files = set()
    lines = [f"impact analysis for: {', '.join(target_files)}\n"]

    for title, levels in (
        ("upstream (callers into these files):", upstream),
        ("downstream (called by these files):", downstream),
    ):
        lines.append(title)
        if not levels:
            lines.append("  (none)")
        else:
            for depth, qualified_list in sorted(levels.items()):
                files = _impact_level_files(qualified_list)
                all_files.update(files)
                shown = files[:_IMPACT_LABELS_PER_LEVEL]
                line = (
                    f"  [depth {depth}] {len(files)} file(s), "
                    f"{len(qualified_list)} symbol(s): " + ", ".join(shown)
                )
                if len(files) > len(shown):
                    line += f" ... +{len(files) - len(shown)} more file(s)"
                lines.append(line)
        lines.append("")

    total_up = sum(len(v) for v in upstream.values())
    total_down = sum(len(v) for v in downstream.values())
    lines.append(
        f"summary: {len(all_files)} files affected, "
        f"{total_up} upstream + {total_down} downstream symbols"
    )

    return "\n".join(lines)


def _load_graph(db, static_only=False):
    prov_filter = f"AND provenance {STATIC_PROVENANCE_SQL}" if static_only else ""
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
        connected = []
        for q, sid in candidates:
            reachable = set()
            frontier = [sid]
            min_depth = None
            for d in range(1, 3):
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
                if min_depth is None and reachable & resolved_ids:
                    min_depth = d
            if min_depth is not None:
                deg = len(adj_fwd.get(sid, [])) + len(adj_bwd.get(sid, []))
                connected.append((min_depth, -deg, len(q), sid, q))
        if connected:
            connected.sort()
            chosen = connected[0]
            return chosen[3], chosen[4], False

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
                if prov == "inferred":
                    edge_label = f"synthesized [via: {via}]" if via else "synthesized"
                elif prov == "speculative":
                    edge_label = "call [speculative resolution]"
                elif prov == "probable":
                    edge_label = "call [name-match]"
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


@_query_scoped
def query_flow_impl(symbols, max_depth=None, max_visited=None, static_only=False):
    if not symbols or len(symbols) < 2:
        return "Error: query_flow requires at least 2 symbols."

    config_values = _config_values()
    max_depth = min(config_values[3], config_values[3] if max_depth is None else max_depth)
    max_visited = min(config_values[4], config_values[4] if max_visited is None else max_visited)
    if static_only is None:
        static_only = config_values[2]

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
