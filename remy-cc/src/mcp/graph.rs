//! Graph queries: BFS callers/callees, impact, and flow.
//! Oracle: remy-src/index_mcp_graph.py + skills/remy-index/impact.py. Level
//! ordering uses sorted qualified names (UTF-8 byte order equals Python's
//! code-point order); the 400-item chunking only bounds SQL parameter counts —
//! the per-depth union is chunk-order independent.

use std::collections::{BTreeMap, HashMap, HashSet};

use rusqlite::Connection;

use super::common::{get_layer, get_line_range, open_db, DB_NOT_FOUND, STATIC_PROVENANCE_SQL};
use super::config::McpConfig;

const IMPACT_LABELS_PER_LEVEL: usize = 5;
const CHUNK: usize = 400;

type Levels = BTreeMap<i64, Vec<String>>;

fn query_strings(db: &Connection, sql: &str, params: &[&str]) -> rusqlite::Result<Vec<String>> {
    let mut stmt = db.prepare(sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| row.get(0))?;
    rows.collect()
}

fn bfs(
    db: &Connection,
    targets: &HashSet<String>,
    max_depth: i64,
    sql_for_chunk: impl Fn(&str, &str) -> String,
    prov_filter: &str,
    double_params: bool,
) -> Levels {
    let mut visited: HashSet<String> = targets.clone();
    let mut current: HashSet<String> = targets.clone();
    let mut levels = Levels::new();

    for depth in 1..=max_depth {
        if current.is_empty() {
            break;
        }
        let current_list: Vec<String> = current.iter().cloned().collect();
        let mut all_rows: HashSet<String> = HashSet::new();
        for chunk in current_list.chunks(CHUNK) {
            let placeholders = vec!["?"; chunk.len()].join(",");
            let sql = sql_for_chunk(&placeholders, prov_filter);
            let params: Vec<&str> = if double_params {
                chunk
                    .iter()
                    .chain(chunk.iter())
                    .map(String::as_str)
                    .collect()
            } else {
                chunk.iter().map(String::as_str).collect()
            };
            if let Ok(rows) = query_strings(db, &sql, &params) {
                all_rows.extend(rows);
            }
        }
        let next_level: Vec<String> = {
            let mut fresh: Vec<String> = all_rows
                .into_iter()
                .filter(|row| !visited.contains(row))
                .collect();
            fresh.sort();
            fresh
        };
        if next_level.is_empty() {
            break;
        }
        visited.extend(next_level.iter().cloned());
        current = next_level.iter().cloned().collect();
        levels.insert(depth, next_level);
    }
    levels
}

fn prov_clause(static_only: bool, alias: &str) -> String {
    if static_only {
        format!("AND {alias}provenance {STATIC_PROVENANCE_SQL}")
    } else {
        String::new()
    }
}

/// impact.bfs_callers.
fn bfs_callers(
    db: &Connection,
    targets: &HashSet<String>,
    max_depth: i64,
    static_only: bool,
) -> Levels {
    let filter = prov_clause(static_only, "");
    bfs(
        db,
        targets,
        max_depth,
        |placeholders, filter| {
            format!(
                "SELECT DISTINCT source_file || '::' || caller FROM edges \
                 WHERE callee_qualified IN ({placeholders}) {filter}"
            )
        },
        &filter,
        false,
    )
}

/// impact.bfs_callees.
fn bfs_callees(
    db: &Connection,
    targets: &HashSet<String>,
    max_depth: i64,
    static_only: bool,
) -> Levels {
    let filter = prov_clause(static_only, "");
    bfs(
        db,
        targets,
        max_depth,
        |placeholders, filter| {
            format!(
                "SELECT DISTINCT callee_qualified FROM edges \
                 WHERE source_file || '::' || caller IN ({placeholders}) \
                 AND callee_qualified IS NOT NULL {filter}"
            )
        },
        &filter,
        false,
    )
}

fn bfs_callers_ambiguous(
    db: &Connection,
    targets: &HashSet<String>,
    max_depth: i64,
    static_only: bool,
) -> Levels {
    let filter = prov_clause(static_only, "e.");
    bfs(
        db,
        targets,
        max_depth,
        |placeholders, filter| {
            format!(
                "SELECT DISTINCT source_file || '::' || caller FROM edges e \
                 WHERE callee_qualified IN ({placeholders}) {filter} \
                 UNION \
                 SELECT DISTINCT e.source_file || '::' || e.caller \
                 FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id \
                 WHERE ec.candidate_qualified IN ({placeholders}) {filter}"
            )
        },
        &filter,
        true,
    )
}

fn bfs_callees_ambiguous(
    db: &Connection,
    targets: &HashSet<String>,
    max_depth: i64,
    static_only: bool,
) -> Levels {
    let filter = prov_clause(static_only, "e.");
    bfs(
        db,
        targets,
        max_depth,
        |placeholders, filter| {
            format!(
                "SELECT DISTINCT callee_qualified FROM edges e \
                 WHERE source_file || '::' || caller IN ({placeholders}) \
                 AND callee_qualified IS NOT NULL {filter} \
                 UNION \
                 SELECT DISTINCT ec.candidate_qualified \
                 FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id \
                 WHERE e.source_file || '::' || e.caller IN ({placeholders}) {filter}"
            )
        },
        &filter,
        true,
    )
}

fn resolve_targets(db: &Connection, symbol: &str) -> HashSet<String> {
    if symbol.contains("::") {
        return HashSet::from([symbol.to_string()]);
    }
    query_strings(
        db,
        "SELECT file_path || '::' || name FROM symbols WHERE name = ?1 OR short_name = ?1",
        &[symbol],
    )
    .map(|rows| rows.into_iter().collect())
    .unwrap_or_default()
}

fn format_bfs_result(
    db: &Connection,
    cfg: &McpConfig,
    title: &str,
    levels: &Levels,
    max_depth: i64,
) -> String {
    if levels.is_empty() {
        return format!("{title} ({max_depth} levels): no results");
    }
    let total: usize = levels.values().map(Vec::len).sum();
    let mut lines = vec![format!("{title} ({max_depth} levels, {total} results)\n")];
    for (depth, qualified_list) in levels {
        let direct = if *depth == 1 { " direct:" } else { "" };
        lines.push(format!("[depth {depth}]{direct}"));
        for (count, q) in (0_i64..).zip(qualified_list.iter()) {
            if count >= cfg.result_limit {
                lines.push(format!(
                    "  ... ({} more)",
                    qualified_list.len() as i64 - count
                ));
                break;
            }
            let fpath = q.split_once("::").map(|(f, _)| f).unwrap_or(q);
            let layer = get_layer(db, fpath);
            let lr = get_line_range(db, q);
            lines.push(format!("  {q}{lr} ({layer})"));
        }
        lines.push(String::new());
    }
    lines.join("\n")
}

fn bfs_query(
    cfg: &McpConfig,
    symbol: &str,
    depth: i64,
    include_ambiguous: bool,
    static_only: bool,
    callers: bool,
) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let depth = depth.min(cfg.bfs_max_depth);
    let targets = resolve_targets(&db, symbol);
    if targets.is_empty() {
        return format!("No symbols found matching '{symbol}'");
    }
    let levels = match (callers, include_ambiguous) {
        (true, true) => bfs_callers_ambiguous(&db, &targets, depth, static_only),
        (true, false) => bfs_callers(&db, &targets, depth, static_only),
        (false, true) => bfs_callees_ambiguous(&db, &targets, depth, static_only),
        (false, false) => bfs_callees(&db, &targets, depth, static_only),
    };
    let title = if callers {
        format!("callers of {symbol}")
    } else {
        format!("callees of {symbol}")
    };
    format_bfs_result(&db, cfg, &title, &levels, depth)
}

pub fn query_callers_impl(
    cfg: &McpConfig,
    symbol: &str,
    depth: i64,
    include_ambiguous: bool,
    static_only: bool,
) -> String {
    bfs_query(cfg, symbol, depth, include_ambiguous, static_only, true)
}

pub fn query_callees_impl(
    cfg: &McpConfig,
    symbol: &str,
    depth: i64,
    include_ambiguous: bool,
    static_only: bool,
) -> String {
    bfs_query(cfg, symbol, depth, include_ambiguous, static_only, false)
}

pub fn query_impact_impl(
    cfg: &McpConfig,
    files: &[String],
    depth_up: i64,
    depth_down: i64,
    include_ambiguous: bool,
    static_only: bool,
) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let depth_up = depth_up.min(cfg.bfs_max_depth);
    let depth_down = depth_down.min(cfg.bfs_max_depth);

    let mut target_files: Vec<String> = Vec::new();
    for f in files {
        let normalized = f.replace('\\', "/");
        let row: Option<String> = db
            .query_row(
                "SELECT path FROM files WHERE path = ?1",
                [&normalized],
                |row| row.get(0),
            )
            .ok();
        if let Some(path) = row {
            target_files.push(path);
        }
    }
    if target_files.is_empty() {
        return format!("No indexed files found matching: {}", files.join(", "));
    }

    let mut seeds: HashSet<String> = HashSet::new();
    for tf in &target_files {
        if let Ok(rows) = query_strings(
            &db,
            "SELECT file_path || '::' || name FROM symbols WHERE file_path = ?1",
            &[tf],
        ) {
            seeds.extend(rows);
        }
    }
    if seeds.is_empty() {
        return format!("No symbols found in: {}", target_files.join(", "));
    }

    let (upstream, downstream) = if include_ambiguous {
        (
            if depth_up > 0 {
                bfs_callers_ambiguous(&db, &seeds, depth_up, static_only)
            } else {
                Levels::new()
            },
            if depth_down > 0 {
                bfs_callees_ambiguous(&db, &seeds, depth_down, static_only)
            } else {
                Levels::new()
            },
        )
    } else {
        (
            if depth_up > 0 {
                bfs_callers(&db, &seeds, depth_up, static_only)
            } else {
                Levels::new()
            },
            if depth_down > 0 {
                bfs_callees(&db, &seeds, depth_down, static_only)
            } else {
                Levels::new()
            },
        )
    };
    format_impact_result(&target_files, &upstream, &downstream)
}

/// index_mcp_graph._impact_level_files: distinct files, first-seen order.
fn impact_level_files(qualified_list: &[String]) -> Vec<String> {
    let mut files = Vec::new();
    let mut seen = HashSet::new();
    for qualified in qualified_list {
        let fpath = qualified
            .split_once("::")
            .map(|(f, _)| f.to_string())
            .unwrap_or_else(|| qualified.clone());
        if seen.insert(fpath.clone()) {
            files.push(fpath);
        }
    }
    files
}

fn format_impact_result(target_files: &[String], upstream: &Levels, downstream: &Levels) -> String {
    let mut all_files: HashSet<String> = HashSet::new();
    let mut lines = vec![format!(
        "impact analysis for: {}\n",
        target_files.join(", ")
    )];

    for (title, levels) in [
        ("upstream (callers into these files):", upstream),
        ("downstream (called by these files):", downstream),
    ] {
        lines.push(title.to_string());
        if levels.is_empty() {
            lines.push("  (none)".to_string());
        } else {
            for (depth, qualified_list) in levels {
                let files = impact_level_files(qualified_list);
                all_files.extend(files.iter().cloned());
                let shown = &files[..files.len().min(IMPACT_LABELS_PER_LEVEL)];
                let mut line = format!(
                    "  [depth {depth}] {} file(s), {} symbol(s): {}",
                    files.len(),
                    qualified_list.len(),
                    shown.join(", ")
                );
                if files.len() > shown.len() {
                    line.push_str(&format!(" ... +{} more file(s)", files.len() - shown.len()));
                }
                lines.push(line);
            }
        }
        lines.push(String::new());
    }

    let total_up: usize = upstream.values().map(Vec::len).sum();
    let total_down: usize = downstream.values().map(Vec::len).sum();
    lines.push(format!(
        "summary: {} files affected, {total_up} upstream + {total_down} downstream symbols",
        all_files.len()
    ));
    lines.join("\n")
}

type Adjacency = HashMap<i64, Vec<(i64, Option<String>, Option<String>)>>;
type ParentMap = HashMap<i64, (Option<i64>, Option<String>, Option<String>)>;
type SymbolInfo = (String, String, String, Option<i64>, String);
type EdgeRow = (String, String, String, Option<String>, Option<String>);

struct Graph {
    adj_fwd: Adjacency,
    adj_bwd: Adjacency,
    name_to_id: HashMap<String, i64>,
    id_to_info: HashMap<i64, SymbolInfo>,
}

fn load_graph(db: &Connection, static_only: bool) -> rusqlite::Result<Graph> {
    let prov_filter = if static_only {
        format!("AND provenance {STATIC_PROVENANCE_SQL}")
    } else {
        String::new()
    };
    let sql = format!(
        "SELECT source_file, caller, callee_qualified, provenance, via \
         FROM edges WHERE callee_qualified IS NOT NULL {prov_filter}"
    );
    let mut stmt = db.prepare(&sql)?;
    let edge_rows: Vec<EdgeRow> = stmt
        .query_map([], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        })?
        .collect::<Result<_, _>>()?;

    let mut stmt = db.prepare(
        "SELECT id, file_path || '::' || name, file_path, name, lineno, type FROM symbols",
    )?;
    let sym_rows: Vec<(i64, String, String, String, Option<i64>, String)> = stmt
        .query_map([], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        })?
        .collect::<Result<_, _>>()?;

    let mut name_to_id = HashMap::new();
    let mut id_to_info = HashMap::new();
    for (sid, qualified, fpath, sname, lineno, stype) in sym_rows {
        name_to_id.insert(qualified.clone(), sid);
        id_to_info.insert(sid, (qualified, fpath, sname, lineno, stype));
    }

    let mut adj_fwd: Adjacency = HashMap::new();
    let mut adj_bwd: Adjacency = HashMap::new();
    for (source_file, caller, callee_qualified, provenance, via) in edge_rows {
        let src_q = format!("{source_file}::{caller}");
        let (Some(&src_id), Some(&tgt_id)) =
            (name_to_id.get(&src_q), name_to_id.get(&callee_qualified))
        else {
            continue;
        };
        adj_fwd
            .entry(src_id)
            .or_default()
            .push((tgt_id, provenance.clone(), via.clone()));
        adj_bwd
            .entry(tgt_id)
            .or_default()
            .push((src_id, provenance, via));
    }
    Ok(Graph {
        adj_fwd,
        adj_bwd,
        name_to_id,
        id_to_info,
    })
}

type PathStep = (i64, Option<String>, Option<String>);

fn bidir_bfs(
    graph: &Graph,
    src_id: i64,
    tgt_id: i64,
    max_depth: i64,
    max_visited: i64,
) -> Option<Vec<PathStep>> {
    if src_id == tgt_id {
        return Some(vec![(src_id, None, None)]);
    }
    let mut fwd_parent: ParentMap = HashMap::from([(src_id, (None, None, None))]);
    let mut bwd_parent: ParentMap = HashMap::from([(tgt_id, (None, None, None))]);
    let mut front_f = vec![src_id];
    let mut front_b = vec![tgt_id];

    for _depth in 0..max_depth {
        if (fwd_parent.len() + bwd_parent.len()) as i64 > max_visited {
            return None;
        }
        if front_f.is_empty() && front_b.is_empty() {
            return None;
        }

        let mut next_f = Vec::new();
        for nid in &front_f {
            for (t, prov, via) in graph.adj_fwd.get(nid).map(Vec::as_slice).unwrap_or(&[]) {
                if !fwd_parent.contains_key(t) {
                    fwd_parent.insert(*t, (Some(*nid), prov.clone(), via.clone()));
                    next_f.push(*t);
                }
                if bwd_parent.contains_key(t) {
                    return Some(reconstruct_path(graph, *t, &fwd_parent, &bwd_parent));
                }
            }
        }
        front_f = next_f;

        let mut next_b = Vec::new();
        for nid in &front_b {
            for (s, prov, via) in graph.adj_bwd.get(nid).map(Vec::as_slice).unwrap_or(&[]) {
                if !bwd_parent.contains_key(s) {
                    bwd_parent.insert(*s, (Some(*nid), prov.clone(), via.clone()));
                    next_b.push(*s);
                }
                if fwd_parent.contains_key(s) {
                    return Some(reconstruct_path(graph, *s, &fwd_parent, &bwd_parent));
                }
            }
        }
        front_b = next_b;
    }
    None
}

fn reconstruct_path(
    graph: &Graph,
    meet: i64,
    fwd_parent: &ParentMap,
    bwd_parent: &ParentMap,
) -> Vec<PathStep> {
    let mut fwd_half: Vec<PathStep> = Vec::new();
    let mut cur = Some(meet);
    while let Some(node) = cur {
        let (parent, prov, via) = fwd_parent[&node].clone();
        fwd_half.push((node, prov, via));
        cur = parent;
    }
    fwd_half.reverse();

    let mut bwd_half: Vec<i64> = Vec::new();
    let mut cur_bwd = bwd_parent[&meet].0;
    while let Some(node) = cur_bwd {
        let (parent, _prov, _via) = bwd_parent[&node].clone();
        bwd_half.push(node);
        cur_bwd = parent;
    }

    let mut result = fwd_half;
    let mut prev_id = meet;
    for nid in bwd_half {
        let mut edge_prov = None;
        let mut edge_via = None;
        for (t, ep, ev) in graph
            .adj_fwd
            .get(&prev_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            if *t == nid {
                edge_prov = ep.clone();
                edge_via = ev.clone();
                break;
            }
        }
        result.push((nid, edge_prov, edge_via));
        prev_id = nid;
    }
    result
}

/// (symbol id, qualified-or-input, ambiguous flag) per index_mcp_graph.
type Resolved = (Option<i64>, String, bool);

fn resolve_flow_symbol(
    sym: &str,
    db: &Connection,
    graph: &Graph,
    resolved_ids: &HashSet<i64>,
    all_tokens: &[String],
) -> Resolved {
    if sym.contains('/') && sym.contains(':') {
        let idx = sym.rfind(':').unwrap();
        let file_part = &sym[..idx];
        let name_part = &sym[idx + 1..];
        let rows = query_strings(
            db,
            "SELECT file_path || '::' || name FROM symbols \
             WHERE file_path LIKE ?1 AND (name = ?2 OR short_name = ?2)",
            &[&format!("%{file_part}%"), name_part],
        )
        .unwrap_or_default();
        if let Some(first) = rows.first() {
            return (graph.name_to_id.get(first).copied(), first.clone(), false);
        }
        return (None, sym.to_string(), false);
    }

    if sym.contains('.') || sym.contains("::") {
        let (class_hint, method_name) = if sym.contains("::") {
            let idx = sym.rfind("::").unwrap();
            (&sym[..idx], &sym[idx + 2..])
        } else {
            let idx = sym.rfind('.').unwrap();
            (&sym[..idx], &sym[idx + 1..])
        };
        let rows = query_strings(
            db,
            "SELECT file_path || '::' || name FROM symbols WHERE (name = ?1 OR short_name = ?1)",
            &[method_name],
        )
        .unwrap_or_default();
        let hint_lower = class_hint.to_lowercase();
        let candidates: Vec<&String> = rows
            .iter()
            .filter(|q| q.to_lowercase().contains(&hint_lower))
            .collect();
        if let Some(first) = candidates.first() {
            return (
                graph.name_to_id.get(*first).copied(),
                (*first).clone(),
                false,
            );
        }
        if let Some(first) = rows.first() {
            return (
                graph.name_to_id.get(first).copied(),
                first.clone(),
                rows.len() > 1,
            );
        }
        return (None, sym.to_string(), false);
    }

    let rows = query_strings(
        db,
        "SELECT file_path || '::' || name FROM symbols WHERE name = ?1 OR short_name = ?1",
        &[sym],
    )
    .unwrap_or_default();

    if rows.is_empty() {
        return (None, sym.to_string(), false);
    }
    if rows.len() == 1 {
        return (
            graph.name_to_id.get(&rows[0]).copied(),
            rows[0].clone(),
            false,
        );
    }

    let candidates: Vec<(String, i64)> = rows
        .iter()
        .filter_map(|q| graph.name_to_id.get(q).map(|sid| (q.clone(), *sid)))
        .collect();
    if candidates.is_empty() {
        return (
            graph.name_to_id.get(&rows[0]).copied(),
            rows[0].clone(),
            true,
        );
    }
    if candidates.len() == 1 {
        return (Some(candidates[0].1), candidates[0].0.clone(), false);
    }

    if !resolved_ids.is_empty() {
        let mut connected: Vec<(i64, i64, usize, i64, String)> = Vec::new();
        for (q, sid) in &candidates {
            let mut reachable: HashSet<i64> = HashSet::new();
            let mut frontier = vec![*sid];
            let mut min_depth: Option<i64> = None;
            for d in 1..3i64 {
                let mut nxt = Vec::new();
                for n in &frontier {
                    for (t, _, _) in graph.adj_fwd.get(n).map(Vec::as_slice).unwrap_or(&[]) {
                        if reachable.insert(*t) {
                            nxt.push(*t);
                        }
                    }
                    for (t, _, _) in graph.adj_bwd.get(n).map(Vec::as_slice).unwrap_or(&[]) {
                        if reachable.insert(*t) {
                            nxt.push(*t);
                        }
                    }
                }
                frontier = nxt;
                if min_depth.is_none() && reachable.iter().any(|r| resolved_ids.contains(r)) {
                    min_depth = Some(d);
                }
            }
            if let Some(depth) = min_depth {
                let deg = (graph.adj_fwd.get(sid).map(Vec::len).unwrap_or(0)
                    + graph.adj_bwd.get(sid).map(Vec::len).unwrap_or(0))
                    as i64;
                connected.push((depth, -deg, q.chars().count(), *sid, q.clone()));
            }
        }
        if !connected.is_empty() {
            connected.sort();
            let chosen = &connected[0];
            return (Some(chosen.3), chosen.4.clone(), false);
        }
    }

    let sym_lower = sym.to_lowercase();
    let other_tokens: HashSet<String> = all_tokens
        .iter()
        .map(|t| t.to_lowercase())
        .filter(|t| *t != sym_lower)
        .collect();
    if !other_tokens.is_empty() {
        for (q, sid) in &candidates {
            let q_lower = q.to_lowercase();
            if other_tokens.iter().any(|tok| q_lower.contains(tok)) {
                return (Some(*sid), q.clone(), false);
            }
        }
    }

    let mut degree_list: Vec<(i64, usize, i64, String)> = candidates
        .iter()
        .map(|(q, sid)| {
            let deg = (graph.adj_fwd.get(sid).map(Vec::len).unwrap_or(0)
                + graph.adj_bwd.get(sid).map(Vec::len).unwrap_or(0)) as i64;
            (deg, q.chars().count(), *sid, q.clone())
        })
        .collect();
    degree_list.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    let ambiguous = degree_list.len() > 1;
    let chosen = &degree_list[0];
    (Some(chosen.2), chosen.3.clone(), ambiguous)
}

fn short_name(qualified: &str) -> &str {
    match qualified.rsplit_once("::") {
        Some((_, name)) => name,
        None => qualified,
    }
}

fn format_flow(
    resolved: &[Resolved],
    segments: &[Option<Vec<PathStep>>],
    graph: &Graph,
    static_only: bool,
    max_depth: i64,
) -> String {
    let total_connected = segments.iter().filter(|s| s.is_some()).count();
    if total_connected == 0 {
        return "No connected paths found among the queried symbols.".to_string();
    }

    let partial = total_connected < segments.len();
    let header = if partial {
        format!(
            "## Flow (partial — {}/{} symbols connected)\n",
            total_connected + 1,
            resolved.len()
        )
    } else {
        "## Flow (call path among queried symbols)\n".to_string()
    };
    let mut lines = vec![header];
    let mut step = 1;

    for (i, (sym_id, sym_qualified, ambiguous)) in resolved.iter().enumerate() {
        let Some(sym_id) = sym_id else {
            lines.push(format!(
                "\n[Unresolved: '{sym_qualified}' not found in index]\n"
            ));
            continue;
        };

        if i > 0 && segments[i - 1].is_none() {
            let prev_name = short_name(&resolved[i - 1].1);
            let cur_name = short_name(sym_qualified);
            lines.push(format!(
                "\n[Break: pair ({prev_name}, {cur_name}) not connected within depth={max_depth}]"
            ));
            if static_only {
                lines.push("[Note: static_only=True excludes synthesized paths]".to_string());
            }
            lines.push(String::new());
        }

        if i == 0 || segments[i - 1].is_none() {
            if let Some((_, fpath, sname, lineno, _)) = graph.id_to_info.get(sym_id) {
                let loc = match super::common::truthy_line(*lineno) {
                    Some(lineno) => format!(":{lineno}"),
                    None => String::new(),
                };
                let amb_note = if *ambiguous {
                    " [ambiguous: resolved by edge_count]"
                } else {
                    ""
                };
                lines.push(format!("{step}. {sname} ({fpath}{loc}){amb_note}"));
                step += 1;
            }
        }

        if i < segments.len() {
            if let Some(path) = &segments[i] {
                for j in 1..path.len() {
                    let (nid, prov, via) = &path[j];
                    let edge_label = match prov.as_deref() {
                        Some("inferred") => match via.as_deref().filter(|v| !v.is_empty()) {
                            Some(via) => format!("synthesized [via: {via}]"),
                            None => "synthesized".to_string(),
                        },
                        Some("speculative") => "call [speculative resolution]".to_string(),
                        Some("probable") => "call [name-match]".to_string(),
                        _ => "call".to_string(),
                    };
                    lines.push(format!("   ↓ {edge_label}"));
                    if let Some((_, fpath, sname, lineno, _)) = graph.id_to_info.get(nid) {
                        let loc = match super::common::truthy_line(*lineno) {
                            Some(lineno) => format!(":{lineno}"),
                            None => String::new(),
                        };
                        let amb_note =
                            if j == path.len() - 1 && i + 1 < resolved.len() && resolved[i + 1].2 {
                                " [ambiguous: resolved by edge_count]"
                            } else {
                                ""
                            };
                        lines.push(format!("{step}. {sname} ({fpath}{loc}){amb_note}"));
                        step += 1;
                    }
                }
            }
        }
    }
    lines.join("\n")
}

pub fn query_flow_impl(
    cfg: &McpConfig,
    symbols: &[String],
    max_depth: i64,
    max_visited: i64,
    static_only: bool,
) -> String {
    if symbols.len() < 2 {
        return "Error: query_flow requires at least 2 symbols.".to_string();
    }
    let max_depth = cfg.flow_max_depth.min(max_depth);
    let max_visited = cfg.flow_max_visited.min(max_visited);

    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let graph = match load_graph(&db, static_only) {
        Ok(graph) => graph,
        Err(error) => return format!("Error: {error}"),
    };
    if graph.adj_fwd.is_empty() && graph.adj_bwd.is_empty() {
        return "No edges in index. Run struct_scan to build the call graph.".to_string();
    }

    let mut all_tokens: Vec<String> = Vec::new();
    for sym in symbols {
        if sym.contains('/') && sym.contains(':') {
            all_tokens.push(sym.rsplit(':').next().unwrap_or(sym).to_string());
        } else if sym.contains('.') {
            all_tokens.extend(sym.split('.').map(str::to_string));
        } else if sym.contains("::") {
            all_tokens.extend(sym.split("::").map(str::to_string));
        } else {
            all_tokens.push(sym.clone());
        }
    }

    let mut resolved: Vec<Resolved> = Vec::new();
    let mut resolved_ids: HashSet<i64> = HashSet::new();
    for sym in symbols {
        let entry = resolve_flow_symbol(sym, &db, &graph, &resolved_ids, &all_tokens);
        if let Some(sid) = entry.0 {
            resolved_ids.insert(sid);
        }
        resolved.push(entry);
    }

    let unresolved: Vec<&str> = resolved
        .iter()
        .filter(|(sid, _, _)| sid.is_none())
        .map(|(_, q, _)| q.as_str())
        .collect();
    if unresolved.len() == resolved.len() {
        return format!("No symbols resolved: {}", unresolved.join(", "));
    }

    let mut segments: Vec<Option<Vec<PathStep>>> = Vec::new();
    for i in 0..resolved.len() - 1 {
        match (resolved[i].0, resolved[i + 1].0) {
            (Some(src), Some(tgt)) => {
                segments.push(bidir_bfs(&graph, src, tgt, max_depth, max_visited));
            }
            _ => segments.push(None),
        }
    }

    format_flow(&resolved, &segments, &graph, static_only, max_depth)
}
