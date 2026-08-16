//! Single-writer SQLite primitives: schema creation, index lifecycle for
//! bulk loads, per-file fact writes, and meta bookkeeping. All functions
//! take the caller's transaction; transaction boundaries (single
//! transaction per scan, rollback on pipeline failure) live in scan.rs.

use crate::facts::FileFacts;
use crate::pyjson;
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::Value;

pub fn open_db(path: &std::path::Path) -> rusqlite::Result<Connection> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    let conn = Connection::open(path)?;
    conn.execute_batch(crate::SCHEMA_SQL)?;
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?1)",
        params![crate::SCHEMA_VERSION],
    )?;
    Ok(conn)
}

/// Drop every secondary index so a bulk load writes plain tables first;
/// rebuild_indexes restores them from the shared DDL afterwards. Automatic
/// (sqlite_autoindex_*) indexes carry NULL sql and are skipped.
pub fn drop_secondary_indexes(tx: &Transaction) -> rusqlite::Result<()> {
    let names: Vec<String> = {
        let mut stmt =
            tx.prepare("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        rows.collect::<Result<_, _>>()?
    };
    for name in names {
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

/// scan_file's write path: upsert the files row, then replace the file's
/// symbols, symbol_occurrences, edges, and patterns.
pub fn write_file_facts(tx: &Transaction, facts: &FileFacts) -> rusqlite::Result<()> {
    if file_unchanged(tx, facts)? {
        return Ok(());
    }

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
    }

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

    Ok(())
}

/// StructScanner._delete_file, phase-1 tables only.
pub fn delete_file(tx: &Transaction, rel_path: &str) -> rusqlite::Result<bool> {
    tx.execute(
        "DELETE FROM symbols WHERE file_path = ?1",
        params![rel_path],
    )?;
    tx.execute(
        "DELETE FROM symbol_occurrences WHERE file_path = ?1",
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
    Ok(removed > 0)
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
            write_file_facts(&tx, &sample_facts("a.c")).unwrap();
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
            write_file_facts(&tx, &sample_facts("a.c")).unwrap();
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
    fn unchanged_file_short_circuits() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let mut conn = open_db(&db_path).unwrap();
        let facts = sample_facts("a.c");
        {
            let tx = conn.transaction().unwrap();
            write_file_facts(&tx, &facts).unwrap();
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
