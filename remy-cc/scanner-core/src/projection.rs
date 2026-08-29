//! retrieval_projection.py replication: current-summary selection, the
//! retrieval_documents upsert/delete family, and stale marking. All fact
//! columns replicate byte-for-byte; content_hash replicates the SHA-256
//! over the identity-compact JSON payload so alternating Python/Rust
//! writers never flap judge_cache keys.

use crate::pyjson;
use rusqlite::{params, Transaction};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

const AVAILABLE_STATUSES: &[&str] = &["ok", "oversized_warn"];
const SKIPPABLE_STATUSES: &[&str] = &["pending", "corrupt", "oversized_hard"];
const BARRIER_STATUS: &str = "stale";

const HASH_FIELDS: &[&str] = &[
    "node_kind",
    "node_ref",
    "language",
    "symbol_type",
    "file_path",
    "name",
    "name_tokens",
    "signature",
    "summary_short",
    "summary_full",
];

#[derive(Debug, Default)]
pub struct CurrentSummary {
    pub id: Option<i64>,
    pub version: Option<i64>,
    pub short: Option<String>,
    pub full: Option<String>,
}

/// `datetime.now().isoformat(timespec='seconds')` equivalent (local time,
/// second precision) — the same shape writer::write_meta stores.
pub fn now_local_iso(tx: &Transaction) -> rusqlite::Result<String> {
    tx.query_row(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime')",
        [],
        |row| row.get(0),
    )
}

/// `retrieval_projection.select_current_summary`: newest-first walk with
/// stale as a hard barrier and pending/corrupt/oversized_hard skipped.
pub fn select_current_summary(
    tx: &Transaction,
    node_kind: &str,
    node_ref: &str,
) -> rusqlite::Result<CurrentSummary> {
    let mut stmt = tx.prepare(
        "SELECT id, version, summary, status FROM summary_versions \
         WHERE node_kind = ?1 AND node_ref = ?2 ORDER BY version DESC",
    )?;
    let rows: Vec<(i64, i64, Option<String>, String)> = stmt
        .query_map(params![node_kind, node_ref], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })?
        .collect::<Result<_, _>>()?;

    for (summary_id, version, summary_json, status) in rows {
        if status == BARRIER_STATUS {
            return Ok(CurrentSummary::default());
        }
        if SKIPPABLE_STATUSES.contains(&status.as_str()) {
            continue;
        }
        if !AVAILABLE_STATUSES.contains(&status.as_str()) {
            continue;
        }
        let Some(payload) = summary_json
            .as_deref()
            .and_then(|text| serde_json::from_str::<Value>(text).ok())
            .and_then(|value| value.as_object().cloned())
        else {
            continue;
        };
        let Some(short) = payload
            .get("short")
            .and_then(Value::as_str)
            .filter(|short| !short.trim().is_empty())
            .map(str::to_string)
        else {
            continue;
        };
        let full = payload
            .get("full")
            .and_then(Value::as_str)
            .map(str::to_string);
        return Ok(CurrentSummary {
            id: Some(summary_id),
            version: Some(version),
            short: Some(short),
            full,
        });
    }
    Ok(CurrentSummary::default())
}

#[derive(Debug)]
struct FactDocument {
    language: Option<String>,
    symbol_type: Option<String>,
    file_path: Option<String>,
    name: Option<String>,
    name_tokens: Option<String>,
    signature: Option<String>,
}

fn symbol_fact_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<FactDocument> {
    Ok(FactDocument {
        file_path: row.get(0)?,
        name: row.get(1)?,
        name_tokens: row.get(2)?,
        signature: row.get(3)?,
        symbol_type: row.get(4)?,
        language: row.get(5)?,
    })
}

fn load_fact_document(
    tx: &Transaction,
    node_kind: &str,
    node_ref: &str,
) -> rusqlite::Result<Option<FactDocument>> {
    match node_kind {
        "symbol" => {
            // The first "::" is the separator for every real on-disk path,
            // so the UNIQUE(file_path, name) index serves this lookup.
            if let Some((file_path, name)) = node_ref.split_once("::") {
                let row = tx
                    .query_row(
                        "SELECT s.file_path, s.name, s.name_tokens, s.args, s.type, \
                         f.language FROM symbols s JOIN files f ON f.path = s.file_path \
                         WHERE s.file_path = ?1 AND s.name = ?2",
                        params![file_path, name],
                        symbol_fact_row,
                    )
                    .map(Some)
                    .or_else(ignore_missing_row)?;
                if row.is_some() {
                    return Ok(row);
                }
            }
            // Fallback for a stored file path containing "::": the original
            // expression predicate stays authoritative.
            tx.query_row(
                "SELECT s.file_path, s.name, s.name_tokens, s.args, s.type, \
                 f.language FROM symbols s JOIN files f ON f.path = s.file_path \
                 WHERE s.file_path || '::' || s.name = ?1",
                params![node_ref],
                symbol_fact_row,
            )
            .map(Some)
            .or_else(ignore_missing_row)
        }
        "file" => tx
            .query_row(
                "SELECT path, language FROM files WHERE path = ?1",
                params![node_ref],
                |row| {
                    let file_path: String = row.get(0)?;
                    let name = file_path
                        .rsplit('/')
                        .next()
                        .unwrap_or(&file_path)
                        .to_string();
                    let tokens = file_path.replace(['/', '_'], " ");
                    Ok(FactDocument {
                        language: row.get(1)?,
                        symbol_type: None,
                        name: Some(name),
                        name_tokens: Some(tokens),
                        signature: None,
                        file_path: Some(file_path),
                    })
                },
            )
            .map(Some)
            .or_else(ignore_missing_row),
        "cluster" => tx
            .query_row(
                "SELECT name, label FROM clusters WHERE name = ?1",
                params![node_ref],
                |row| {
                    let name: String = row.get(0)?;
                    let label: Option<String> = row.get(1)?;
                    let tokens = name.replace(['/', '_'], " ");
                    Ok(FactDocument {
                        language: None,
                        symbol_type: None,
                        file_path: None,
                        name: Some(label.unwrap_or_else(|| name.clone())),
                        name_tokens: Some(tokens),
                        signature: None,
                    })
                },
            )
            .map(Some)
            .or_else(ignore_missing_row),
        _ => Ok(None),
    }
}

fn ignore_missing_row<T>(error: rusqlite::Error) -> rusqlite::Result<Option<T>> {
    match error {
        rusqlite::Error::QueryReturnedNoRows => Ok(None),
        other => Err(other),
    }
}

fn optional(value: &Option<String>) -> Value {
    match value {
        Some(text) => Value::String(text.clone()),
        None => Value::Null,
    }
}

/// `retrieval_projection._document_hash`: SHA-256 over the sorted compact
/// JSON of the ten hash fields.
fn document_hash(
    node_kind: &str,
    node_ref: &str,
    document: &FactDocument,
    summary: &CurrentSummary,
) -> String {
    let mut payload = Map::new();
    for field in HASH_FIELDS {
        let value = match *field {
            "node_kind" => Value::String(node_kind.to_string()),
            "node_ref" => Value::String(node_ref.to_string()),
            "language" => optional(&document.language),
            "symbol_type" => optional(&document.symbol_type),
            "file_path" => optional(&document.file_path),
            "name" => optional(&document.name),
            "name_tokens" => optional(&document.name_tokens),
            "signature" => optional(&document.signature),
            "summary_short" => optional(&summary.short),
            "summary_full" => optional(&summary.full),
            _ => Value::Null,
        };
        payload.insert((*field).to_string(), value);
    }
    let encoded = pyjson::dumps_identity(&Value::Object(payload));
    let mut hasher = Sha256::new();
    hasher.update(encoded.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// `retrieval_projection.refresh_node`: update-then-insert upsert of the
/// current document, deleting the row when the fact side is gone.
pub fn refresh_node(tx: &Transaction, node_kind: &str, node_ref: &str) -> rusqlite::Result<()> {
    let Some(document) = load_fact_document(tx, node_kind, node_ref)? else {
        return delete_node(tx, node_kind, node_ref);
    };
    let summary = select_current_summary(tx, node_kind, node_ref)?;
    let content_hash = document_hash(node_kind, node_ref, &document, &summary);
    let updated_at = now_local_iso(tx)?;
    let updated = tx.execute(
        "UPDATE retrieval_documents SET language=?1, symbol_type=?2, file_path=?3, \
         name=?4, name_tokens=?5, signature=?6, summary_short=?7, summary_full=?8, \
         content_hash=?9, source_version=?10, updated_at=?11 \
         WHERE node_kind=?12 AND node_ref=?13",
        params![
            document.language,
            document.symbol_type,
            document.file_path,
            document.name,
            document.name_tokens,
            document.signature,
            summary.short,
            summary.full,
            content_hash,
            summary.version,
            updated_at,
            node_kind,
            node_ref,
        ],
    )?;
    if updated == 0 {
        tx.execute(
            "INSERT INTO retrieval_documents (\
             node_kind, node_ref, language, symbol_type, file_path, name, \
             name_tokens, signature, summary_short, summary_full, content_hash, \
             source_version, updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
            params![
                node_kind,
                node_ref,
                document.language,
                document.symbol_type,
                document.file_path,
                document.name,
                document.name_tokens,
                document.signature,
                summary.short,
                summary.full,
                content_hash,
                summary.version,
                updated_at,
            ],
        )?;
    }
    Ok(())
}

pub fn delete_node(tx: &Transaction, node_kind: &str, node_ref: &str) -> rusqlite::Result<()> {
    tx.execute(
        "DELETE FROM retrieval_documents WHERE node_kind = ?1 AND node_ref = ?2",
        params![node_kind, node_ref],
    )?;
    Ok(())
}

pub fn delete_file_nodes(
    tx: &Transaction,
    file_path: &str,
    symbol_refs: &[String],
) -> rusqlite::Result<()> {
    for node_ref in symbol_refs {
        delete_node(tx, "symbol", node_ref)?;
    }
    delete_node(tx, "file", file_path)
}

/// `retrieval_projection.mark_current_summary_stale`.
pub fn mark_current_summary_stale(
    tx: &Transaction,
    node_kind: &str,
    node_ref: &str,
) -> rusqlite::Result<bool> {
    let current = select_current_summary(tx, node_kind, node_ref)?;
    let Some(summary_id) = current.id else {
        refresh_node(tx, node_kind, node_ref)?;
        return Ok(false);
    };
    tx.execute(
        "UPDATE summary_versions SET status = 'stale' WHERE id = ?1",
        params![summary_id],
    )?;
    refresh_node(tx, node_kind, node_ref)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::writer::open_db;

    fn seed_symbol(tx: &Transaction) {
        tx.execute(
            "INSERT INTO files (path, struct_hash, language) VALUES ('a.rs', 'h', 'RustParser')",
            [],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, hash, name_tokens) \
             VALUES ('a.rs', 'save', 'save', 'function', '()', 1, 'x', 'save')",
            [],
        )
        .unwrap();
    }

    #[test]
    fn symbol_lookup_covers_separator_collisions_via_fallback() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        // Name containing "::" resolves through the column-equality split.
        tx.execute(
            "INSERT INTO files (path, struct_hash, language) VALUES ('f.cpp', 'h', 'CCppParser')",
            [],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, hash, name_tokens) \
             VALUES ('f.cpp', 'ns::run', 'run', 'function', '()', 1, 'x', 'ns run')",
            [],
        )
        .unwrap();
        // Pathological stored path containing "::" resolves via the
        // expression fallback (the first-"::" split misses).
        tx.execute(
            "INSERT INTO files (path, struct_hash, language) VALUES ('a::b.rs', 'h', 'RustParser')",
            [],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO symbols (file_path, name, short_name, type, args, lineno, hash, name_tokens) \
             VALUES ('a::b.rs', 'save', 'save', 'function', '()', 1, 'x', 'save')",
            [],
        )
        .unwrap();

        for (node_ref, file_path) in [("f.cpp::ns::run", "f.cpp"), ("a::b.rs::save", "a::b.rs")] {
            refresh_node(&tx, "symbol", node_ref).unwrap();
            let stored: String = tx
                .query_row(
                    "SELECT file_path FROM retrieval_documents \
                     WHERE node_kind='symbol' AND node_ref=?1",
                    params![node_ref],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(stored, file_path, "{node_ref}");
        }
    }

    #[test]
    fn refresh_inserts_then_updates_and_delete_removes() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        seed_symbol(&tx);
        refresh_node(&tx, "symbol", "a.rs::save").unwrap();
        let (hash1, short): (String, Option<String>) = tx
            .query_row(
                "SELECT content_hash, summary_short FROM retrieval_documents \
                 WHERE node_kind='symbol' AND node_ref='a.rs::save'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(short, None);
        assert_eq!(hash1.len(), 64);

        tx.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) \
             VALUES ('symbol', 'a.rs::save', 1, '{\"short\": \"Saves.\", \"full\": null}', 'ok', 't')",
            [],
        )
        .unwrap();
        refresh_node(&tx, "symbol", "a.rs::save").unwrap();
        let (hash2, short2, version): (String, Option<String>, Option<i64>) = tx
            .query_row(
                "SELECT content_hash, summary_short, source_version FROM retrieval_documents \
                 WHERE node_kind='symbol' AND node_ref='a.rs::save'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(short2.as_deref(), Some("Saves."));
        assert_eq!(version, Some(1));
        assert_ne!(hash1, hash2);

        tx.execute("DELETE FROM symbols WHERE name='save'", [])
            .unwrap();
        refresh_node(&tx, "symbol", "a.rs::save").unwrap();
        let count: i64 = tx
            .query_row("SELECT COUNT(*) FROM retrieval_documents", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn summary_state_machine_barriers_and_skips() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        for (version, summary, status) in [
            (1, Some("{\"short\": \"v1\", \"full\": null}"), "ok"),
            (2, Some("{\"short\": \"v2\", \"full\": \"body\"}"), "ok"),
            (3, None, "pending"),
        ] {
            tx.execute(
                "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) \
                 VALUES ('file', 'a.rs', ?1, ?2, ?3, 't')",
                params![version, summary, status],
            )
            .unwrap();
        }
        let current = select_current_summary(&tx, "file", "a.rs").unwrap();
        assert_eq!(current.short.as_deref(), Some("v2"));
        assert_eq!(current.full.as_deref(), Some("body"));

        tx.execute(
            "UPDATE summary_versions SET status='stale' WHERE version=2",
            [],
        )
        .unwrap();
        let barred = select_current_summary(&tx, "file", "a.rs").unwrap();
        assert_eq!(barred.id, None);
        assert_eq!(barred.short, None);
    }

    #[test]
    fn mark_stale_updates_projection_and_reports_transition() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        seed_symbol(&tx);
        tx.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) \
             VALUES ('symbol', 'a.rs::save', 1, '{\"short\": \"Saves.\", \"full\": null}', 'ok', 't')",
            [],
        )
        .unwrap();
        assert!(mark_current_summary_stale(&tx, "symbol", "a.rs::save").unwrap());
        let status: String = tx
            .query_row(
                "SELECT status FROM summary_versions WHERE version=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "stale");
        let short: Option<String> = tx
            .query_row(
                "SELECT summary_short FROM retrieval_documents WHERE node_ref='a.rs::save'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(short, None);
        assert!(!mark_current_summary_stale(&tx, "symbol", "a.rs::save").unwrap());
    }
}
