//! query_dependencies: file-level import/include relations the call graph
//! does not express. Rust single-implementation tool — excluded from the
//! H.4 differential matrix (docs/MCP_RUST_PARITY_BASELINE.md §4.2); the
//! dedicated suite tests/test_mcp_dependencies.py is the acceptance surface.
//! Derivation reuses scanner_core::postprocess::derive_import_bindings so
//! query-time and scan-time unique-suffix semantics cannot drift.

use std::collections::{BTreeMap, BTreeSet, HashSet};

use rusqlite::Connection;
use scanner_core::postprocess::derive_import_bindings;

use super::common::{open_db, DB_NOT_FOUND};
use super::config::McpConfig;

struct ImportGraph {
    /// file -> merged, deduplicated import targets (stored resolved paths
    /// plus unique-suffix derivation supplements), lexicographic order.
    forward: BTreeMap<String, BTreeSet<String>>,
    /// Paths present in the files table; anything else is dangling.
    indexed: HashSet<String>,
}

fn build_import_graph(db: &Connection) -> ImportGraph {
    let mut forward: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut indexed: HashSet<String> = HashSet::new();

    let rows: Vec<(String, Option<String>)> = db
        .prepare("SELECT path, imports FROM files ORDER BY path")
        .and_then(|mut stmt| {
            stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect()
        })
        .unwrap_or_default();
    for (path, imports_json) in &rows {
        indexed.insert(path.clone());
        let targets: BTreeSet<String> = imports_json
            .as_deref()
            .and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok())
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default()
            .iter()
            .filter_map(serde_json::Value::as_str)
            .map(str::to_string)
            .collect();
        if !targets.is_empty() {
            forward.insert(path.clone(), targets);
        }
    }

    if let Ok(derivation) = derive_import_bindings(db) {
        for (source, supplements) in derivation.supplements {
            forward.entry(source).or_default().extend(supplements);
        }
    }

    ImportGraph { forward, indexed }
}

type Levels = Vec<(i64, Vec<String>)>;

/// BFS over the import graph; `reverse` walks importers (up direction).
/// Seeds never re-enter; each node appears only in its first-reached level;
/// levels are lexicographically sorted by construction (BTreeSet).
fn bfs(graph: &ImportGraph, seeds: &[String], depth: i64, reverse: bool) -> Levels {
    let mut levels = Levels::new();
    let mut visited: HashSet<String> = seeds.iter().cloned().collect();
    let mut frontier: BTreeSet<String> = seeds.iter().cloned().collect();

    let mut level = 0;
    while level < depth && !frontier.is_empty() {
        level += 1;
        let mut next: BTreeSet<String> = BTreeSet::new();
        for node in &frontier {
            if reverse {
                for (source, targets) in &graph.forward {
                    if targets.contains(node) && !visited.contains(source) {
                        next.insert(source.clone());
                    }
                }
            } else if let Some(targets) = graph.forward.get(node) {
                for target in targets {
                    if !visited.contains(target) {
                        next.insert(target.clone());
                    }
                }
            }
        }
        if next.is_empty() {
            break;
        }
        visited.extend(next.iter().cloned());
        levels.push((level, next.iter().cloned().collect()));
        frontier = next;
    }
    levels
}

fn render_section(lines: &mut Vec<String>, title: &str, levels: &Levels, graph: &ImportGraph) {
    lines.push(title.to_string());
    if levels.is_empty() {
        lines.push("  (none)".to_string());
    } else {
        for (depth, files) in levels {
            let labels: Vec<String> = files
                .iter()
                .map(|path| {
                    if graph.indexed.contains(path) {
                        path.clone()
                    } else {
                        format!("{path} (not indexed)")
                    }
                })
                .collect();
            lines.push(format!(
                "  [depth {depth}] {} file(s): {}",
                files.len(),
                labels.join(", ")
            ));
        }
    }
    lines.push(String::new());
}

pub fn query_dependencies_impl(
    cfg: &McpConfig,
    files: &[String],
    direction: &str,
    depth: i64,
) -> String {
    if !matches!(direction, "up" | "down" | "both") {
        return "Error: direction must be one of up/down/both.".to_string();
    }
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let depth = depth.min(cfg.bfs_max_depth);

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
            if !target_files.contains(&path) {
                target_files.push(path);
            }
        }
    }
    if target_files.is_empty() {
        return format!("No indexed files found matching: {}", files.join(", "));
    }

    let graph = build_import_graph(&db);

    let mut lines = vec![format!(
        "dependency analysis for: {}\n",
        target_files.join(", ")
    )];
    let mut summary_parts: Vec<String> = Vec::new();

    if direction == "up" || direction == "both" {
        let upstream = bfs(&graph, &target_files, depth, true);
        let total: usize = upstream.iter().map(|(_, files)| files.len()).sum();
        render_section(
            &mut lines,
            "imported by (upstream importers):",
            &upstream,
            &graph,
        );
        summary_parts.push(format!("{total} upstream file(s)"));
    }
    if direction == "down" || direction == "both" {
        let downstream = bfs(&graph, &target_files, depth, false);
        let total: usize = downstream.iter().map(|(_, files)| files.len()).sum();
        render_section(
            &mut lines,
            "imports (downstream dependencies):",
            &downstream,
            &graph,
        );
        summary_parts.push(format!("{total} downstream file(s)"));
    }

    lines.push(format!("summary: {}", summary_parts.join(", ")));
    lines.join("\n")
}
