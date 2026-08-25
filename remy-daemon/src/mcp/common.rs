//! Shared database access and summary lookup for the Rust MCP server.
//! Oracle: remy-src/index_mcp_common.py plus the impact.py helpers the query
//! modules import. Connection pragmas replicate the Python MCP form exactly
//! (WAL + busy_timeout=3000); the scanner's 128 MiB cache_size is a measured
//! postprocess-only setting and is deliberately not inherited here.

use std::path::Path;

use rusqlite::{Connection, OpenFlags};

pub const DB_NOT_FOUND: &str =
    "Error: logic_index.db not found. Run /remy-index to initialize the project index.";
/// schema.py STATIC_PROVENANCE_SQL.
pub const STATIC_PROVENANCE_SQL: &str = "IN ('definite','probable')";

const AVAILABLE_STATUSES: &[&str] = &["ok", "oversized_warn"];
const SKIPPABLE_STATUSES: &[&str] = &["pending", "corrupt", "oversized_hard"];
const BARRIER_STATUS: &str = "stale";

pub fn open_db(db_path: &Path) -> Option<Connection> {
    if !db_path.exists() {
        return None;
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .ok()?;
    conn.pragma_update(None, "journal_mode", "WAL").ok()?;
    conn.pragma_update(None, "busy_timeout", 3000).ok()?;
    Some(conn)
}

/// index_mcp_common.get_latest_summary: short/full/status of the current
/// summary, `None` when no version row exists at all.
#[derive(Debug, Default, Clone)]
pub struct Summary {
    pub short: Option<String>,
    pub full: Option<String>,
    pub status: Option<String>,
}

pub fn get_latest_summary(db: &Connection, node_kind: &str, node_ref: &str) -> Option<Summary> {
    let mut stmt = db
        .prepare(
            "SELECT id, version, summary, status FROM summary_versions \
             WHERE node_kind = ?1 AND node_ref = ?2 ORDER BY version DESC",
        )
        .ok()?;
    let rows: Vec<(i64, Option<String>, String)> = stmt
        .query_map([node_kind, node_ref], |row| {
            Ok((row.get(0)?, row.get(2)?, row.get(3)?))
        })
        .ok()?
        .filter_map(Result::ok)
        .collect();
    let latest_status = rows.first().map(|(_, _, status)| status.clone());

    for (_, summary_json, status) in &rows {
        if status == BARRIER_STATUS {
            return Some(Summary {
                short: None,
                full: None,
                status: Some(BARRIER_STATUS.to_string()),
            });
        }
        if SKIPPABLE_STATUSES.contains(&status.as_str()) {
            continue;
        }
        if !AVAILABLE_STATUSES.contains(&status.as_str()) {
            continue;
        }
        let Some(payload) = summary_json
            .as_deref()
            .and_then(|text| serde_json::from_str::<serde_json::Value>(text).ok())
            .and_then(|value| value.as_object().cloned())
        else {
            continue;
        };
        let Some(short) = payload
            .get("short")
            .and_then(serde_json::Value::as_str)
            .filter(|short| !short.trim().is_empty())
            .map(str::to_string)
        else {
            continue;
        };
        let full = payload
            .get("full")
            .and_then(serde_json::Value::as_str)
            .map(str::to_string);
        return Some(Summary {
            short: Some(short),
            full,
            status: Some(status.clone()),
        });
    }
    latest_status.map(|status| Summary {
        short: None,
        full: None,
        status: Some(status),
    })
}

/// impact.get_layer.
pub fn get_layer(db: &Connection, file_path: &str) -> String {
    db.query_row(
        "SELECT layer FROM files WHERE path = ?1",
        [file_path],
        |row| row.get::<_, Option<String>>(0),
    )
    .ok()
    .flatten()
    .unwrap_or_else(|| "Unknown".to_string())
}

/// impact.get_line_range: " [L{start}-L{end}]" / " [L{start}]" / "".
pub fn get_line_range(db: &Connection, qualified: &str) -> String {
    let Some((fpath, name)) = qualified.split_once("::") else {
        return String::new();
    };
    let row: Option<(Option<i64>, Option<i64>)> = db
        .query_row(
            "SELECT lineno, end_lineno FROM symbols WHERE file_path = ?1 AND name = ?2",
            [fpath, name],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .ok();
    // Python truthiness: 0 and NULL are both falsy for lineno columns.
    let truthy = |value: Option<i64>| value.filter(|v| *v != 0);
    match row {
        Some((start, end)) => match (truthy(start), truthy(end)) {
            (Some(start), Some(end)) => format!(" [L{start}-L{end}]"),
            (Some(start), None) => format!(" [L{start}]"),
            _ => String::new(),
        },
        None => String::new(),
    }
}

/// Shared Python-truthiness helper for lineno-style columns.
pub fn truthy_line(value: Option<i64>) -> Option<i64> {
    value.filter(|v| *v != 0)
}

/// Python truthiness for the freshness-warning prefix and `Error:` passthrough.
pub fn with_freshness(warning: &str, result: String) -> String {
    if result.starts_with("Error:") {
        return result;
    }
    if warning.is_empty() {
        return result;
    }
    format!("{warning}\n\n{result}")
}
