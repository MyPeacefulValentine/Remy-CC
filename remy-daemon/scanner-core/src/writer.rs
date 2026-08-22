//! Single-writer SQLite primitives: schema creation with the
//! initialize_database version guard, index lifecycle for bulk loads,
//! per-file fact writes with the scan_file summary/projection semantics,
//! and meta bookkeeping. All functions take the caller's transaction;
//! transaction boundaries (single transaction per scan, rollback on
//! pipeline failure) live in scan.rs.

use crate::facts::FileFacts;
use crate::projection;
use crate::pyjson;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

/// migrations.initialize_database guard, fail-closed: a fresh (or empty)
/// database gets the schema and version stamp; an existing database with a
/// missing or non-current version is refused unchanged — the migration
/// ladder stays a Python-side single owner (run the Python scanner once to
/// migrate, then rescan here).
pub fn open_db(path: &std::path::Path) -> Result<Connection, String> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    let db_existed = path.exists();
    let conn = Connection::open(path).map_err(|e| format!("open failed: {e}"))?;
    // 128 MiB page cache: −29% on the tee ×8 postprocess segment (F.3,
    // 2026-08-21); the only measured tier, hence no config key.
    conn.execute_batch("PRAGMA cache_size = -131072")
        .map_err(|e| format!("cache_size pragma failed: {e}"))?;
    let table_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
            [],
            |row| row.get(0),
        )
        .map_err(|e| format!("schema probe failed: {e}"))?;
    if !db_existed || table_count == 0 {
        conn.execute_batch(crate::SCHEMA_SQL)
            .map_err(|e| format!("schema create failed: {e}"))?;
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?1)",
            params![crate::SCHEMA_VERSION],
        )
        .map_err(|e| format!("version stamp failed: {e}"))?;
        return Ok(conn);
    }
    let version: Option<String> = conn
        .query_row("SELECT value FROM meta WHERE key='version'", [], |row| {
            row.get(0)
        })
        .optional()
        .unwrap_or(None);
    match version {
        None => Err(format!(
            "Existing logic_index.db at {} has no schema version. \
             The database is preserved unchanged.",
            path.display()
        )),
        Some(version) if version != crate::SCHEMA_VERSION => Err(format!(
            "logic_index.db at {} has schema version {version}, expected {}. \
             The database is preserved unchanged; migrate it with the Python \
             scanner first.",
            path.display(),
            crate::SCHEMA_VERSION
        )),
        Some(_) => {
            conn.execute_batch(crate::SCHEMA_SQL)
                .map_err(|e| format!("schema replay failed: {e}"))?;
            Ok(conn)
        }
    }
}

/// edges and patterns have no composite key covering their per-file
/// DELETE; dropping these two would degrade those DELETEs to full-table
/// scans (quadratic over a full scan).
const BULK_KEPT_INDEXES: &[&str] = &["idx_edges_source_file", "idx_patterns_file"];

/// Drop every secondary index (minus the DELETE-serving whitelist) so a
/// bulk load writes plain tables first; rebuild_indexes restores them from
/// the shared DDL afterwards. Automatic (sqlite_autoindex_*) indexes carry
/// NULL sql and are skipped.
pub fn drop_secondary_indexes(tx: &Transaction) -> rusqlite::Result<()> {
    let names: Vec<String> = {
        let mut stmt =
            tx.prepare("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        rows.collect::<Result<_, _>>()?
    };
    for name in names {
        if BULK_KEPT_INDEXES.contains(&name.as_str()) {
            continue;
        }
        tx.execute_batch(&format!("DROP INDEX IF EXISTS \"{name}\""))?;
    }
    Ok(())
}

/// Recreate whatever the bulk load dropped (every DDL statement is
/// IF NOT EXISTS, so replaying the full schema only restores indexes).
pub fn rebuild_indexes(tx: &Transaction) -> rusqlite::Result<()> {
    tx.execute_batch(crate::SCHEMA_SQL)
}

/// scan_file's unchanged-file short circuit: identical struct_hash and
/// parser identity leave the stored rows untouched.
pub fn file_unchanged(tx: &Transaction, facts: &FileFacts) -> rusqlite::Result<bool> {
    let existing: Option<(String, String, String, String)> = tx
        .query_row(
            "SELECT struct_hash, parser_contract_version, parser_backend, \
             parser_environment FROM files WHERE path = ?1",
            params![facts.rel_path],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    Ok(existing
        == Some((
            facts.struct_hash.clone(),
            facts.identity.contract_version.clone(),
            facts.identity.backend.clone(),
            facts.identity.environment.clone(),
        )))
}

/// Per-file change record aggregated by scan_files into ScanDelta.
#[derive(Debug, Default)]
pub struct FileDelta {
    pub changed: bool,
    /// The files row was newly inserted.
    pub created: bool,
    /// Old ∪ new symbol names and short names of the file.
    pub names: Vec<String>,
}

/// scan_file's write path: upsert the files row, then replace the file's
/// symbols, symbol_occurrences, edges, and patterns, carrying the
/// summary-invalidation and retrieval-projection side effects (symbol hash
/// change marks the symbol summary stale, symbol-set change marks the file
/// summary stale, initial summaries from docstrings / the small-function
/// filter, refresh_node per symbol and file).
pub fn write_file_facts(
    tx: &Transaction,
    facts: &FileFacts,
    filter_small: bool,
) -> rusqlite::Result<FileDelta> {
    if file_unchanged(tx, facts)? {
        return Ok(FileDelta::default());
    }

    let mut delta_names: Vec<String> = Vec::new();
    let old_hashes: HashMap<String, Option<String>> = {
        let mut stmt =
            tx.prepare("SELECT name, short_name, hash FROM symbols WHERE file_path = ?1")?;
        let rows: Vec<(String, String, Option<String>)> = stmt
            .query_map(params![facts.rel_path], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?))
            })?
            .collect::<Result<_, _>>()?;
        rows.into_iter()
            .map(|(name, short_name, hash)| {
                delta_names.push(name.clone());
                delta_names.push(short_name);
                (name, hash)
            })
            .collect()
    };
    let old_symbol_refs: HashSet<String> = old_hashes
        .keys()
        .map(|name| format!("{}::{}", facts.rel_path, name))
        .collect();
    let existing_versions: HashMap<String, i64> = {
        let mut stmt = tx.prepare(
            "SELECT node_ref, MAX(version) FROM summary_versions \
             WHERE node_kind = 'symbol' AND node_ref LIKE ?1 GROUP BY node_ref",
        )?;
        let rows: Vec<(String, i64)> = stmt
            .query_map(params![format!("{}::%", facts.rel_path)], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })?
            .collect::<Result<_, _>>()?;
        rows.into_iter().collect()
    };

    let exists: Option<i64> = tx
        .query_row(
            "SELECT 1 FROM files WHERE path = ?1",
            params![facts.rel_path],
            |row| row.get(0),
        )
        .optional()?;
    if exists.is_some() {
        tx.execute(
            "UPDATE files SET struct_hash=?1, language=?2, layer=?3, imports=?4, \
             parser_contract_version=?5, parser_backend=?6, parser_environment=?7, \
             import_bindings=?8 WHERE path=?9",
            params![
                facts.struct_hash,
                facts.language_id,
                facts.layer,
                facts.imports_json,
                facts.identity.contract_version,
                facts.identity.backend,
                facts.identity.environment,
                facts.import_bindings_json,
                facts.rel_path,
            ],
        )?;
    } else {
        tx.execute(
            "INSERT INTO files (path, struct_hash, language, layer, imports, \
             parser_contract_version, parser_backend, parser_environment, \
             import_bindings) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            params![
                facts.rel_path,
                facts.struct_hash,
                facts.language_id,
                facts.layer,
                facts.imports_json,
                facts.identity.contract_version,
                facts.identity.backend,
                facts.identity.environment,
                facts.import_bindings_json,
            ],
        )?;
    }

    tx.execute(
        "DELETE FROM symbols WHERE file_path = ?1",
        params![facts.rel_path],
    )?;
    tx.execute(
        "DELETE FROM symbol_occurrences WHERE file_path = ?1",
        params![facts.rel_path],
    )?;
    // Orphan guard: foreign_keys is off, so candidates left behind here
    // would survive the targeted incremental reset forever.
    tx.execute(
        "DELETE FROM edge_candidates WHERE edge_id IN \
         (SELECT id FROM edges WHERE source_file = ?1)",
        params![facts.rel_path],
    )?;
    tx.execute(
        "DELETE FROM edges WHERE source_file = ?1",
        params![facts.rel_path],
    )?;
    tx.execute(
        "DELETE FROM patterns WHERE file_path = ?1",
        params![facts.rel_path],
    )?;

    for occurrence in &facts.occurrences {
        let symbol = &occurrence.symbol;
        tx.execute(
            "INSERT INTO symbol_occurrences \
             (file_path, name, occurrence_index, type, args, lineno, end_lineno, hash, \
             is_canonical, conflict_kind, selection_reason) \
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
            params![
                facts.rel_path,
                symbol.name,
                occurrence.occurrence_index,
                symbol.sym_type,
                symbol.args,
                symbol.lineno,
                symbol.end_lineno,
                occurrence.hash,
                occurrence.is_canonical as i64,
                occurrence.conflict_kind,
                occurrence.selection_reason,
            ],
        )?;
    }

    let language = crate::language::Language::from_language_id(&facts.language_id)
        .expect("language_id written by scan is always registered");
    let now_iso = projection::now_local_iso(tx)?;
    let mut new_symbol_refs: HashSet<String> = HashSet::new();
    for symbol in &facts.canonical_symbols {
        let hash = crate::hashes::symbol_hash(&language.symbol_hash_input(&symbol.source_segment));
        let short_name = symbol
            .name
            .rsplit('.')
            .next()
            .unwrap_or(&symbol.name)
            .to_string();
        let bases_json: Option<String> = symbol.bases.as_ref().and_then(|bases| {
            if bases.is_empty() {
                None
            } else {
                Some(pyjson::dumps_default(&Value::Array(
                    bases.iter().map(|b| Value::String(b.clone())).collect(),
                )))
            }
        });
        let tokens = crate::hashes::tokenize_symbol(&symbol.name);
        tx.execute(
            "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, \
             end_lineno, hash, bases, name_tokens) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            params![
                facts.rel_path,
                symbol.name,
                short_name,
                symbol.sym_type,
                symbol.args,
                symbol.lineno,
                symbol.end_lineno,
                hash,
                bases_json,
                tokens,
            ],
        )?;

        delta_names.push(symbol.name.clone());
        delta_names.push(short_name.clone());
        let node_ref = format!("{}::{}", facts.rel_path, symbol.name);
        new_symbol_refs.insert(node_ref.clone());
        let hash_unchanged = old_hashes.get(&symbol.name) == Some(&Some(hash));
        let has_existing_version = existing_versions.contains_key(&node_ref);
        if hash_unchanged && has_existing_version {
            projection::refresh_node(tx, "symbol", &node_ref)?;
            continue;
        }
        if has_existing_version {
            projection::mark_current_summary_stale(tx, "symbol", &node_ref)?;
        }

        let mut initial_summary: Option<String> = None;
        // Python truthiness: an empty docstring falls through to the
        // small-function branch, exactly like a missing one.
        if let Some(docstring) = symbol.docstring.as_deref().filter(|d| !d.is_empty()) {
            let lines: Vec<&str> = docstring
                .split('\n')
                .map(str::trim)
                .filter(|line| !line.is_empty())
                .collect();
            if !lines.is_empty() {
                initial_summary = Some(format!("[Doc] {}", lines[..lines.len().min(3)].join(" ")));
            }
        } else if filter_small && symbol.source_segment.lines().count() < 3 {
            initial_summary = Some("Small utility function.".to_string());
        }

        if let Some(short) = initial_summary {
            let new_version = existing_versions.get(&node_ref).copied().unwrap_or(0) + 1;
            let payload = pyjson::dumps_summary(&json!({"short": short, "full": null}));
            tx.execute(
                "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) \
                 VALUES ('symbol', ?1, ?2, ?3, 'ok', ?4)",
                params![node_ref, new_version, payload, now_iso],
            )?;
        }
        projection::refresh_node(tx, "symbol", &node_ref)?;
    }

    for removed_ref in old_symbol_refs.difference(&new_symbol_refs) {
        projection::delete_node(tx, "symbol", removed_ref)?;
    }
    if exists.is_some() && old_symbol_refs != new_symbol_refs {
        projection::mark_current_summary_stale(tx, "file", &facts.rel_path)?;
    }
    projection::refresh_node(tx, "file", &facts.rel_path)?;

    for edge in &facts.edges {
        tx.execute(
            "INSERT INTO edges (source_file, caller, callee, line, provenance, \
             synthesized_from, via, call_form) VALUES (?1,?2,?3,?4,NULL,NULL,NULL,?5)",
            params![
                facts.rel_path,
                edge.caller,
                edge.callee,
                edge.line,
                edge.call_form
            ],
        )?;
    }

    for pattern in &facts.patterns {
        tx.execute(
            "INSERT INTO patterns (file_path, pattern_type, signal_name, handler, line, \
             metadata) VALUES (?1,?2,?3,?4,?5,?6)",
            params![
                facts.rel_path,
                pattern.pattern_type,
                pattern.signal_name,
                pattern.handler,
                pattern.line,
                pattern.metadata_json,
            ],
        )?;
    }

    delta_names.sort();
    delta_names.dedup();
    Ok(FileDelta {
        changed: true,
        created: exists.is_none(),
        names: delta_names,
    })
}

/// StructScanner._delete_file: retrieval-projection cascade first, then
/// the fact rows (manual per-table deletes replicate the oracle's
/// foreign-key cascade — this connection never enables foreign_keys).
/// Returns the removed file's FileDelta, or None when no files row existed.
pub fn delete_file(tx: &Transaction, rel_path: &str) -> rusqlite::Result<Option<FileDelta>> {
    let mut names: Vec<String> = Vec::new();
    let symbol_refs: Vec<String> = {
        let mut stmt =
            tx.prepare("SELECT name, short_name FROM symbols WHERE file_path = ?1 ORDER BY name")?;
        let rows: Vec<(String, String)> = stmt
            .query_map(params![rel_path], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?;
        rows.into_iter()
            .map(|(name, short_name)| {
                names.push(name.clone());
                names.push(short_name);
                format!("{rel_path}::{name}")
            })
            .collect()
    };
    projection::delete_file_nodes(tx, rel_path, &symbol_refs)?;
    tx.execute(
        "DELETE FROM symbols WHERE file_path = ?1",
        params![rel_path],
    )?;
    tx.execute(
        "DELETE FROM symbol_occurrences WHERE file_path = ?1",
        params![rel_path],
    )?;
    // Same orphan guard as write_file_facts: candidates go before edges.
    tx.execute(
        "DELETE FROM edge_candidates WHERE edge_id IN \
         (SELECT id FROM edges WHERE source_file = ?1)",
        params![rel_path],
    )?;
    tx.execute(
        "DELETE FROM edges WHERE source_file = ?1",
        params![rel_path],
    )?;
    tx.execute(
        "DELETE FROM patterns WHERE file_path = ?1",
        params![rel_path],
    )?;
    let removed = tx.execute("DELETE FROM files WHERE path = ?1", params![rel_path])?;
    if removed == 0 {
        return Ok(None);
    }
    names.sort();
    names.dedup();
    Ok(Some(FileDelta {
        changed: true,
        created: false,
        names,
    }))
}

/// Diagnostic meta rows: last_updated (local time, second precision, same
/// shape as datetime.now().isoformat(timespec='seconds')) and file_count.
pub fn write_meta(tx: &Transaction, file_count: Option<usize>) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', \
         strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))",
        [],
    )?;
    if let Some(count) = file_count {
        tx.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('file_count', ?1)",
            params![count.to_string()],
        )?;
    }
    Ok(())
}

/// `scanner._resolve_git_head` + the meta source_commit write on the full
/// scan's success path: try the scan root, then the directory inferred
/// from the first indexed file (multi-repo workspaces); silently skip when
/// no git context resolves.
pub fn write_source_commit(tx: &Transaction, root_dir: &std::path::Path) -> rusqlite::Result<()> {
    let mut candidates = vec![root_dir.to_path_buf()];
    let first_file: Option<String> = tx
        .query_row("SELECT path FROM files LIMIT 1", [], |row| row.get(0))
        .optional()?;
    if let Some(rel) = first_file {
        let joined = root_dir.join(rel);
        if let Some(parent) = joined.parent() {
            candidates.push(parent.to_path_buf());
        }
    }
    for candidate in candidates {
        if !candidate.is_dir() {
            continue;
        }
        let output = std::process::Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(&candidate)
            .output();
        let Ok(output) = output else {
            continue;
        };
        if !output.status.success() {
            continue;
        }
        let head = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if head.is_empty() {
            continue;
        }
        tx.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('source_commit', ?1)",
            params![head],
        )?;
        return Ok(());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::{CacheIdentity, FileFacts};

    fn sample_facts(rel_path: &str) -> FileFacts {
        FileFacts {
            rel_path: rel_path.to_string(),
            struct_hash: "h1".to_string(),
            language_id: "CCppParser".to_string(),
            layer: "Core".to_string(),
            imports_json: "[]".to_string(),
            import_bindings_json: "[]".to_string(),
            identity: CacheIdentity {
                contract_version: "1".to_string(),
                backend: "c-tree-sitter".to_string(),
                environment: "{}".to_string(),
            },
            canonical_symbols: Vec::new(),
            occurrences: Vec::new(),
            edges: Vec::new(),
            patterns: Vec::new(),
        }
    }

    #[test]
    fn dropped_transaction_leaves_empty_tables() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let mut conn = open_db(&db_path).unwrap();
        {
            let tx = conn.transaction().unwrap();
            drop_secondary_indexes(&tx).unwrap();
            write_file_facts(&tx, &sample_facts("a.c"), false).unwrap();
            // Simulated pipeline failure: the transaction is dropped
            // without commit.
        }
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM files", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
        let index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND sql IS NOT NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            index_count > 0,
            "index drop must roll back with the transaction"
        );
    }

    #[test]
    fn commit_persists_and_indexes_are_rebuilt() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let mut conn = open_db(&db_path).unwrap();
        {
            let tx = conn.transaction().unwrap();
            drop_secondary_indexes(&tx).unwrap();
            write_file_facts(&tx, &sample_facts("a.c"), false).unwrap();
            rebuild_indexes(&tx).unwrap();
            write_meta(&tx, Some(1)).unwrap();
            tx.commit().unwrap();
        }
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM files", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND sql IS NOT NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(index_count > 0);
        let version: String = conn
            .query_row("SELECT value FROM meta WHERE key='version'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(version, crate::SCHEMA_VERSION);
    }

    #[test]
    fn bulk_kept_indexes_survive_drop() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("state.db")).unwrap();
        let tx = conn.transaction().unwrap();
        drop_secondary_indexes(&tx).unwrap();
        let surviving: Vec<String> = {
            let mut stmt = tx
                .prepare("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
                .unwrap();
            let rows = stmt.query_map([], |row| row.get::<_, String>(0)).unwrap();
            rows.collect::<Result<_, _>>().unwrap()
        };
        for kept in BULK_KEPT_INDEXES {
            assert!(surviving.iter().any(|name| name == kept), "{kept}");
        }
        assert!(!surviving.iter().any(|name| name == "idx_symbols_name"));
    }

    #[test]
    fn unchanged_file_short_circuits() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let mut conn = open_db(&db_path).unwrap();
        let facts = sample_facts("a.c");
        {
            let tx = conn.transaction().unwrap();
            write_file_facts(&tx, &facts, false).unwrap();
            tx.commit().unwrap();
        }
        {
            let tx = conn.transaction().unwrap();
            assert!(file_unchanged(&tx, &facts).unwrap());
            let mut changed = facts.clone();
            changed.struct_hash = "h2".to_string();
            assert!(!file_unchanged(&tx, &changed).unwrap());
        }
    }
}
