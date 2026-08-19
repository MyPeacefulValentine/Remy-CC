//! _compute_file_kinds and _detect_clusters replication, including the
//! cluster-side summary invalidation (member-set change marks the cluster
//! summary stale), node_change_counters seeding, and orphan cleanup.

use crate::projection;
use crate::pyjson;
use crate::rconfig::PostprocessConfig;
use rusqlite::{params, params_from_iter, Transaction};
use serde_json::Value;
use std::collections::{BTreeSet, HashMap};

/// `scanner._compute_kind_hint`.
fn compute_kind_hint(config: &PostprocessConfig, sym_count: i64, intra_edges: i64) -> &'static str {
    if sym_count < config.file_kind_min_symbols {
        return "trivial";
    }
    let density = if sym_count > 0 {
        intra_edges as f64 / sym_count as f64
    } else {
        0.0
    };
    if density < config.file_kind_low_cohesion_threshold {
        return "low_cohesion";
    }
    "cohesive"
}

/// `StructScanner._compute_file_kinds`.
pub fn compute_file_kinds(tx: &Transaction, config: &PostprocessConfig) -> rusqlite::Result<()> {
    let rows: Vec<(String, i64, i64)> = {
        let mut stmt = tx.prepare(
            "SELECT f.path, \
             (SELECT COUNT(*) FROM symbols s WHERE s.file_path = f.path) AS sym_count, \
             (SELECT COUNT(*) FROM edges e WHERE e.source_file = f.path AND e.callee_file = f.path) AS intra_edges \
             FROM files f ORDER BY f.path",
        )?;
        let collected = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        collected
    };
    for (path, sym_count, intra_edges) in rows {
        let hint = compute_kind_hint(config, sym_count, intra_edges);
        tx.execute(
            "UPDATE files SET kind_hint = ?1 WHERE path = ?2",
            params![hint, path],
        )?;
    }
    Ok(())
}

/// Insertion-ordered grouping (Python dict semantics).
fn group_paths<'a>(
    paths: impl Iterator<Item = &'a String>,
    key_of: impl Fn(&str) -> String,
) -> Vec<(String, Vec<String>)> {
    let mut order: Vec<String> = Vec::new();
    let mut groups: HashMap<String, Vec<String>> = HashMap::new();
    for path in paths {
        let key = key_of(path);
        if !groups.contains_key(&key) {
            order.push(key.clone());
        }
        groups.entry(key).or_default().push(path.clone());
    }
    order
        .into_iter()
        .map(|key| {
            let members = groups.remove(&key).unwrap_or_default();
            (key, members)
        })
        .collect()
}

/// `StructScanner._detect_clusters`.
pub fn detect_clusters(tx: &Transaction, config: &PostprocessConfig) -> rusqlite::Result<()> {
    let density_threshold = config.cluster_density_threshold;
    let max_size = config.cluster_max_size as usize;
    let entry_count = config.cluster_entry_count;

    let all_paths: Vec<String> = {
        let mut stmt = tx.prepare("SELECT path FROM files ORDER BY path")?;
        let collected = stmt
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        collected
    };
    let groups = group_paths(all_paths.iter(), |path| {
        let parts: Vec<&str> = path.split('/').collect();
        if parts.len() > 1 {
            parts[0].to_string()
        } else {
            "_root".to_string()
        }
    });

    let existing_names: Vec<String> = {
        let mut stmt = tx.prepare("SELECT name FROM clusters ORDER BY name")?;
        let collected = stmt
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        collected
    };
    let mut existing_members: HashMap<String, BTreeSet<String>> = HashMap::new();
    for name in &existing_names {
        let mut stmt = tx.prepare(
            "SELECT cm.file_path FROM cluster_members cm \
             JOIN clusters c ON c.id = cm.cluster_id WHERE c.name = ?1 \
             ORDER BY cm.file_path",
        )?;
        let members: BTreeSet<String> = stmt
            .query_map(params![name], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        existing_members.insert(name.clone(), members);
    }

    tx.execute("DELETE FROM cluster_members", [])?;
    tx.execute("DELETE FROM clusters", [])?;

    for (gname, members) in groups {
        if members.len() < 2 {
            continue;
        }
        let final_groups: Vec<(String, Vec<String>)> = if members.len() > max_size {
            group_paths(members.iter(), |path| {
                let parts: Vec<&str> = path.split('/').collect();
                if parts.len() > 2 {
                    format!("{}/{}", parts[0], parts[1])
                } else {
                    gname.clone()
                }
            })
        } else {
            vec![(gname.clone(), members)]
        };

        for (cluster_name, cluster_files) in final_groups {
            if cluster_files.len() < 2 {
                continue;
            }
            let placeholders = vec!["?"; cluster_files.len()].join(",");
            let edge_count: i64 = {
                let sql = format!(
                    "SELECT COUNT(*) FROM edges WHERE source_file IN ({placeholders}) \
                     AND callee_file IN ({placeholders})"
                );
                let mut stmt = tx.prepare(&sql)?;
                let bound: Vec<&str> = cluster_files
                    .iter()
                    .chain(cluster_files.iter())
                    .map(String::as_str)
                    .collect();
                stmt.query_row(params_from_iter(bound), |row| row.get(0))?
            };
            let density = edge_count as f64 / cluster_files.len() as f64;
            if density < density_threshold {
                continue;
            }

            let entry_symbols: Vec<String> = {
                let sql = format!(
                    "SELECT callee_qualified, COUNT(*) as cnt FROM edges \
                     WHERE callee_file IN ({placeholders}) AND callee_qualified IS NOT NULL \
                     GROUP BY callee_qualified \
                     ORDER BY cnt DESC, callee_qualified ASC LIMIT ?"
                );
                let mut stmt = tx.prepare(&sql)?;
                let mut bound: Vec<String> = cluster_files.clone();
                bound.push(entry_count.to_string());
                let collected = stmt
                    .query_map(params_from_iter(bound.iter().map(String::as_str)), |row| {
                        row.get::<_, String>(0)
                    })?
                    .collect::<Result<_, _>>()?;
                collected
            };
            let entry_symbols = if entry_symbols.is_empty() {
                vec![format!("{}::*", cluster_files[0])]
            } else {
                entry_symbols
            };

            tx.execute(
                "INSERT INTO clusters (name, label, entry_symbols, file_count) VALUES (?1,?2,?3,?4)",
                params![
                    cluster_name,
                    Option::<String>::None,
                    pyjson::dumps_default(&Value::Array(
                        entry_symbols.iter().map(|s| Value::String(s.clone())).collect(),
                    )),
                    cluster_files.len() as i64,
                ],
            )?;
            let cluster_id = tx.last_insert_rowid();
            for file_path in &cluster_files {
                tx.execute(
                    "INSERT INTO cluster_members (cluster_id, file_path) VALUES (?1,?2)",
                    params![cluster_id, file_path],
                )?;
            }
            tx.execute(
                "INSERT OR IGNORE INTO node_change_counters \
                 (node_kind, node_ref, child_change_count, leaf_descendant_count) \
                 VALUES ('cluster', ?1, 0, 0)",
                params![cluster_name],
            )?;
            let current_set: BTreeSet<String> = cluster_files.iter().cloned().collect();
            if existing_members.get(&cluster_name) != Some(&current_set) {
                projection::mark_current_summary_stale(tx, "cluster", &cluster_name)?;
            }
            projection::refresh_node(tx, "cluster", &cluster_name)?;
        }
    }

    let current_refs: BTreeSet<String> = {
        let mut stmt = tx.prepare("SELECT name FROM clusters ORDER BY name")?;
        let collected = stmt
            .query_map([], |row| row.get(0))?
            .collect::<Result<BTreeSet<String>, _>>()?;
        collected
    };
    for removed in existing_names
        .iter()
        .filter(|name| !current_refs.contains(*name))
    {
        projection::delete_node(tx, "cluster", removed)?;
    }
    let counter_refs: Vec<String> = {
        let mut stmt = tx.prepare(
            "SELECT node_ref FROM node_change_counters \
             WHERE node_kind = 'cluster' ORDER BY node_ref",
        )?;
        let collected = stmt
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?;
        collected
    };
    for node_ref in counter_refs {
        if !current_refs.contains(&node_ref) {
            tx.execute(
                "DELETE FROM node_change_counters WHERE node_kind = 'cluster' AND node_ref = ?1",
                params![node_ref],
            )?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::writer::open_db;

    fn config() -> PostprocessConfig {
        PostprocessConfig {
            filter_small: false,
            cluster_density_threshold: 0.5,
            cluster_max_size: 15,
            cluster_entry_count: 3,
            synth_interface_fanout_cap: 10,
            synth_event_fanout_cap: 20,
            resolve_fanout_cap: 10,
            resolve_score_same_file: 2,
            resolve_score_direct_import: 1,
            resolve_score_global: 0,
            file_kind_min_symbols: 5,
            file_kind_low_cohesion_threshold: 0.25,
            scan_lock_timeout: 30.0,
            struct_scan_timeout: 60,
        }
    }

    fn seed_two_file_cluster(tx: &Transaction) {
        for path in ["pkg/a.py", "pkg/b.py"] {
            tx.execute(
                "INSERT INTO files (path, struct_hash) VALUES (?1, 'h')",
                params![path],
            )
            .unwrap();
        }
        tx.execute(
            "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified, provenance) \
             VALUES ('pkg/a.py', 'main', 'run', 'pkg/b.py', 'pkg/b.py::run', 'definite')",
            [],
        )
        .unwrap();
    }

    #[test]
    fn detects_cluster_and_marks_member_change_stale() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap();
        let tx = conn.transaction().unwrap();
        seed_two_file_cluster(&tx);
        detect_clusters(&tx, &config()).unwrap();
        let (name, entries, count): (String, String, i64) = tx
            .query_row(
                "SELECT name, entry_symbols, file_count FROM clusters",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(name, "pkg");
        assert_eq!(entries, r#"["pkg/b.py::run"]"#);
        assert_eq!(count, 2);
        let counter: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM node_change_counters WHERE node_kind='cluster' AND node_ref='pkg'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(counter, 1);

        tx.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) \
             VALUES ('cluster', 'pkg', 1, '{\"short\": \"pkg cluster\", \"full\": null}', 'ok', 't')",
            [],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO files (path, struct_hash) VALUES ('pkg/c.py', 'h')",
            [],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified, provenance) \
             VALUES ('pkg/a.py', 'main', 'go', 'pkg/c.py', 'pkg/c.py::go', 'definite')",
            [],
        )
        .unwrap();
        detect_clusters(&tx, &config()).unwrap();
        let status: String = tx
            .query_row(
                "SELECT status FROM summary_versions WHERE node_kind='cluster'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "stale");
    }

    #[test]
    fn removed_cluster_cleans_projection_and_counters() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap();
        let tx = conn.transaction().unwrap();
        seed_two_file_cluster(&tx);
        detect_clusters(&tx, &config()).unwrap();
        tx.execute("DELETE FROM edges", []).unwrap();
        detect_clusters(&tx, &config()).unwrap();
        for (table, filter) in [
            ("clusters", "1=1"),
            ("node_change_counters", "node_kind='cluster'"),
            ("retrieval_documents", "node_kind='cluster'"),
        ] {
            let count: i64 = tx
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE {filter}"),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(count, 0, "{table}");
        }
    }

    #[test]
    fn kind_hints_cover_all_three_bands() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap();
        let tx = conn.transaction().unwrap();
        tx.execute(
            "INSERT INTO files (path, struct_hash) VALUES ('tiny.py', 'h'), ('loose.py', 'h'), ('dense.py', 'h')",
            [],
        )
        .unwrap();
        for index in 0..5 {
            for path in ["loose.py", "dense.py"] {
                tx.execute(
                    "INSERT INTO symbols (file_path, name, short_name, type, lineno, hash, name_tokens) \
                     VALUES (?1, ?2, ?2, 'function', 1, 'x', '')",
                    params![path, format!("f{index}")],
                )
                .unwrap();
            }
        }
        for index in 0..3 {
            tx.execute(
                "INSERT INTO edges (source_file, caller, callee, callee_file, provenance) \
                 VALUES ('dense.py', ?1, 'g', 'dense.py', 'definite')",
                params![format!("f{index}")],
            )
            .unwrap();
        }
        compute_file_kinds(&tx, &config()).unwrap();
        let hints: Vec<(String, String)> = {
            let mut stmt = tx
                .prepare("SELECT path, kind_hint FROM files ORDER BY path")
                .unwrap();
            stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap()
        };
        assert_eq!(
            hints,
            vec![
                ("dense.py".to_string(), "cohesive".to_string()),
                ("loose.py".to_string(), "low_cohesion".to_string()),
                ("tiny.py".to_string(), "trivial".to_string()),
            ]
        );
    }
}
