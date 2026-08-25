//! Fact queries: symbols, files, clusters, patterns.
//! Oracle: remy-src/index_mcp_facts.py — rendering is byte-identical, so the
//! Python formatting quirks (Python truthiness on 0/"", `L{None}` when a
//! lineno is NULL) are replicated deliberately.

use rusqlite::Connection;

use super::common::{get_latest_summary, get_layer, open_db, truthy_line, Summary, DB_NOT_FOUND};
use super::config::McpConfig;

fn py_line(value: Option<i64>) -> String {
    match value {
        Some(v) => v.to_string(),
        None => "None".to_string(),
    }
}

struct SymbolRow {
    file_path: String,
    name: String,
    symbol_type: String,
    args: Option<String>,
    lineno: Option<i64>,
    end_lineno: Option<i64>,
}

fn resolve_symbol(
    db: &Connection,
    name: &str,
    file: Option<&str>,
    limit: i64,
) -> rusqlite::Result<Vec<SymbolRow>> {
    const COLUMNS: &str =
        "SELECT file_path, name, type, args, lineno, end_lineno FROM symbols WHERE ";
    const ORDER: &str = " ORDER BY file_path, name, COALESCE(lineno, 0)";
    let map = |row: &rusqlite::Row| {
        Ok(SymbolRow {
            file_path: row.get(0)?,
            name: row.get(1)?,
            symbol_type: row.get(2)?,
            args: row.get(3)?,
            lineno: row.get(4)?,
            end_lineno: row.get(5)?,
        })
    };
    let rows: Vec<SymbolRow> = if let Some((fpath, sname)) = name.split_once("::") {
        let sql = format!("{COLUMNS}file_path = ?1 AND name = ?2{ORDER}");
        let mut stmt = db.prepare(&sql)?;
        let rows = stmt.query_map([fpath, sname], map)?;
        rows.collect::<Result<_, _>>()?
    } else if let Some(file) = file {
        let sql = format!("{COLUMNS}file_path = ?1 AND (name = ?2 OR short_name = ?2){ORDER}");
        let mut stmt = db.prepare(&sql)?;
        let rows = stmt.query_map([file, name], map)?;
        rows.collect::<Result<_, _>>()?
    } else {
        let sql = format!("{COLUMNS}name = ?1 OR short_name = ?1{ORDER}");
        let mut stmt = db.prepare(&sql)?;
        let rows = stmt.query_map([name], map)?;
        rows.collect::<Result<_, _>>()?
    };
    Ok(rows.into_iter().take(limit.max(0) as usize).collect())
}

fn nonempty(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|v| !v.is_empty())
}

fn summary_short(summary: &Option<Summary>) -> Option<&str> {
    summary
        .as_ref()
        .and_then(|s| s.short.as_deref())
        .filter(|s| !s.is_empty())
}

fn summary_full(summary: &Option<Summary>) -> Option<&str> {
    summary
        .as_ref()
        .and_then(|s| s.full.as_deref())
        .filter(|s| !s.is_empty())
}

fn summary_status_not_ok(summary: &Option<Summary>) -> Option<&str> {
    summary
        .as_ref()
        .and_then(|s| s.status.as_deref())
        .filter(|s| !s.is_empty() && *s != "ok")
}

pub fn query_symbol_impl(cfg: &McpConfig, name: &str, file: Option<&str>) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let rows = match resolve_symbol(&db, name, file, cfg.result_limit) {
        Ok(rows) => rows,
        Err(error) => return format!("Error: {error}"),
    };
    if rows.is_empty() {
        return format!("No symbols found matching '{name}'");
    }
    let mut lines = vec![format!(
        "symbols matching '{name}' ({} results)\n",
        rows.len()
    )];
    for row in &rows {
        let layer = get_layer(&db, &row.file_path);
        let mut loc = format!("L{}", py_line(row.lineno));
        if let Some(end) = truthy_line(row.end_lineno) {
            loc.push_str(&format!("-L{end}"));
        }
        let sig = match nonempty(&row.args) {
            Some(args) => format!("({args})"),
            None => String::new(),
        };
        lines.push(format!(
            "  [{}] {}::{}{}  {}:{} ({})",
            row.symbol_type, row.file_path, row.name, sig, row.file_path, loc, layer
        ));
        let summary =
            get_latest_summary(&db, "symbol", &format!("{}::{}", row.file_path, row.name));
        if let Some(short) = summary_short(&summary) {
            lines.push(format!("        {short}"));
        }
    }
    lines.join("\n")
}

pub fn query_symbol_summary_impl(cfg: &McpConfig, name: &str, file: Option<&str>) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let rows = match resolve_symbol(&db, name, file, cfg.result_limit) {
        Ok(rows) => rows,
        Err(error) => return format!("Error: {error}"),
    };
    if rows.is_empty() {
        return format!("No symbols found matching '{name}'");
    }
    let mut lines = vec![format!("summary for '{name}'\n")];
    for row in &rows {
        let sig = match nonempty(&row.args) {
            Some(args) => format!("({args})"),
            None => String::new(),
        };
        lines.push(format!(
            "  [{}] {}::{}{}  L{}",
            row.symbol_type,
            row.file_path,
            row.name,
            sig,
            py_line(row.lineno)
        ));
        let summary =
            get_latest_summary(&db, "symbol", &format!("{}::{}", row.file_path, row.name));
        if let Some(short) = summary_short(&summary) {
            lines.push(format!("  summary: {short}"));
            if let Some(full) = summary_full(&summary) {
                lines.push(format!("  detail: {full}"));
            }
        } else {
            lines.push("  summary: (no summary available)".to_string());
        }
        lines.push(String::new());
    }
    lines.join("\n")
}

pub fn query_patterns_impl(
    cfg: &McpConfig,
    pattern_type: Option<&str>,
    signal_name: Option<&str>,
    file: Option<&str>,
) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let mut conditions: Vec<&str> = Vec::new();
    let mut params: Vec<String> = Vec::new();
    if let Some(value) = pattern_type {
        conditions.push("pattern_type = ?");
        params.push(value.to_string());
    }
    if let Some(value) = signal_name {
        conditions.push("signal_name = ?");
        params.push(value.to_string());
    }
    if let Some(value) = file {
        conditions.push("file_path = ?");
        params.push(value.to_string());
    }
    let where_clause = if conditions.is_empty() {
        "1=1".to_string()
    } else {
        conditions.join(" AND ")
    };
    let sql = format!(
        "SELECT file_path, pattern_type, signal_name, handler, line \
         FROM patterns WHERE {where_clause} \
         ORDER BY file_path, pattern_type, COALESCE(signal_name, ''), \
         COALESCE(handler, ''), COALESCE(line, 0) LIMIT ?"
    );
    params.push(cfg.result_limit.to_string());

    let mut stmt = match db.prepare(&sql) {
        Ok(stmt) => stmt,
        Err(error) => return format!("Error: {error}"),
    };
    type PatternRow = (String, String, Option<String>, Option<String>, Option<i64>);
    let rows: Vec<PatternRow> =
        match stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
            ))
        }) {
            Ok(rows) => rows.filter_map(Result::ok).collect(),
            Err(error) => return format!("Error: {error}"),
        };

    if rows.is_empty() {
        let mut filters: Vec<String> = Vec::new();
        if let Some(value) = pattern_type {
            filters.push(format!("type={value}"));
        }
        if let Some(value) = signal_name {
            filters.push(format!("signal={value}"));
        }
        if let Some(value) = file {
            filters.push(format!("file={value}"));
        }
        let suffix = if filters.is_empty() {
            String::new()
        } else {
            format!(" ({})", filters.join(", "))
        };
        return format!("No patterns found{suffix}");
    }

    let mut lines = vec![format!(
        "event/callback patterns ({} results)\n",
        rows.len()
    )];
    for (fpath, ptype, signal, handler, line) in &rows {
        let loc = match truthy_line(*line) {
            Some(line) => format!("L{line}"),
            None => String::new(),
        };
        let signal = nonempty(signal).unwrap_or("?");
        let handler = nonempty(handler).unwrap_or("?");
        lines.push(format!("  [{ptype}] {signal} -> {handler}  {fpath}:{loc}"));
    }
    lines.join("\n")
}

pub fn query_cluster_summary_impl(cfg: &McpConfig, name: Option<&str>) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let map = |row: &rusqlite::Row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, i64>(3)?,
        ))
    };
    let rows: Vec<(String, Option<String>, Option<String>, i64)> = match name {
        Some(name) => {
            let mut stmt = match db.prepare(
                "SELECT name, label, entry_symbols, file_count FROM clusters WHERE name = ?1",
            ) {
                Ok(stmt) => stmt,
                Err(error) => return format!("Error: {error}"),
            };
            let collected: Vec<_> = match stmt.query_map([name], map) {
                Ok(rows) => rows.filter_map(Result::ok).collect(),
                Err(error) => return format!("Error: {error}"),
            };
            collected
        }
        None => {
            let mut stmt = match db.prepare(
                "SELECT name, label, entry_symbols, file_count FROM clusters \
                 ORDER BY file_count DESC, name",
            ) {
                Ok(stmt) => stmt,
                Err(error) => return format!("Error: {error}"),
            };
            let collected: Vec<_> = match stmt.query_map([], map) {
                Ok(rows) => rows.filter_map(Result::ok).collect(),
                Err(error) => return format!("Error: {error}"),
            };
            collected
        }
    };
    if rows.is_empty() {
        let suffix = match name {
            Some(name) => format!(" matching '{name}'"),
            None => String::new(),
        };
        return format!("No clusters found{suffix}");
    }
    let mut lines: Vec<String> = Vec::new();
    for (cluster_name, label, entry_json, file_count) in &rows {
        let summary = get_latest_summary(&db, "cluster", cluster_name);
        let mut header = format!("## {cluster_name} ({file_count} files)");
        if let Some(label) = nonempty(label) {
            if label != cluster_name {
                header.push_str(&format!("  [alias: {label}]"));
            }
        }
        lines.push(header);
        if let Some(short) = summary_short(&summary) {
            lines.push(format!("  short: {short}"));
        }
        if let Some(full) = summary_full(&summary) {
            lines.push(format!("  full: {full}"));
        }
        let entry_symbols: Vec<String> = entry_json
            .as_deref()
            .and_then(|text| serde_json::from_str::<Vec<String>>(text).ok())
            .unwrap_or_default();
        if !entry_symbols.is_empty() {
            let shown: Vec<&str> = entry_symbols.iter().take(5).map(String::as_str).collect();
            lines.push(format!("  entry_symbols: {}", shown.join(", ")));
        }
        if let Some(status) = summary_status_not_ok(&summary) {
            lines.push(format!("  status: {status}"));
        }
        lines.push(String::new());
    }
    lines.join("\n").trim_end().to_string()
}

pub fn query_file_summary_impl(cfg: &McpConfig, file: &str) -> String {
    if file.is_empty() {
        return "Error: file path is required".to_string();
    }
    let file = file.replace('\\', "/");
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let known: Option<String> = db
        .query_row("SELECT path FROM files WHERE path = ?1", [&file], |row| {
            row.get(0)
        })
        .ok();
    if known.is_none() {
        return format!("No file '{file}' in index. Run /remy-index to scan.");
    }
    let mut stmt = match db.prepare(
        "SELECT name, type, lineno, end_lineno FROM symbols \
         WHERE file_path = ?1 ORDER BY lower(name), name, COALESCE(lineno, 0)",
    ) {
        Ok(stmt) => stmt,
        Err(error) => return format!("Error: {error}"),
    };
    let symbol_rows: Vec<(String, String, Option<i64>, Option<i64>)> = match stmt
        .query_map([&file], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        }) {
        Ok(rows) => rows.filter_map(Result::ok).collect(),
        Err(error) => return format!("Error: {error}"),
    };
    let symbol_count = symbol_rows.len();
    let layer = get_layer(&db, &file);
    let summary = get_latest_summary(&db, "file", &file);
    let mut lines = vec![format!("## {file} ({symbol_count} symbols, layer={layer})")];
    if let Some(short) = summary_short(&summary) {
        lines.push(format!("  short: {short}"));
        if let Some(full) = summary_full(&summary) {
            lines.push(format!("  full: {full}"));
        }
    } else {
        lines.push("  summary: (no summary available)".to_string());
    }
    if let Some(status) = summary_status_not_ok(&summary) {
        lines.push(format!("  status: {status}"));
    }
    if symbol_rows.is_empty() {
        lines.push("  key symbols: (none)".to_string());
    } else {
        let shown = &symbol_rows[..symbol_rows.len().min(cfg.result_limit.max(0) as usize)];
        lines.push("  key symbols:".to_string());
        for (sname, stype, lineno, end_lineno) in shown {
            let loc = match truthy_line(*lineno) {
                Some(start) => {
                    let mut loc = format!("  L{start}");
                    if let Some(end) = truthy_line(*end_lineno) {
                        loc.push_str(&format!("-L{end}"));
                    }
                    loc
                }
                None => String::new(),
            };
            lines.push(format!("    - [{stype}] {sname}{loc}"));
        }
        let remaining = symbol_count - shown.len();
        if remaining > 0 {
            lines.push(format!("    ... (+{remaining} more)"));
        }
    }
    lines.join("\n")
}

pub fn query_cluster_files_impl(cfg: &McpConfig, cluster: &str, with_summary: bool) -> String {
    if cluster.is_empty() {
        return "Error: cluster name is required".to_string();
    }
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    let row: Option<(i64, Option<String>, i64)> = db
        .query_row(
            "SELECT id, label, file_count FROM clusters WHERE name = ?1",
            [cluster],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .ok();
    let Some((cluster_id, label, file_count)) = row else {
        return format!(
            "No cluster '{cluster}' found. Use query_cluster_summary() to list all clusters."
        );
    };
    let mut stmt = match db.prepare(
        "SELECT cm.file_path, f.layer FROM cluster_members cm \
         JOIN files f ON cm.file_path = f.path \
         WHERE cm.cluster_id = ?1 ORDER BY cm.file_path",
    ) {
        Ok(stmt) => stmt,
        Err(error) => return format!("Error: {error}"),
    };
    let member_rows: Vec<(String, Option<String>)> =
        match stmt.query_map([cluster_id], |row| Ok((row.get(0)?, row.get(1)?))) {
            Ok(rows) => rows.filter_map(Result::ok).collect(),
            Err(error) => return format!("Error: {error}"),
        };
    if member_rows.is_empty() {
        return format!("Cluster '{cluster}' has no member files.");
    }
    let mut header = format!("## {cluster} ({file_count} files)");
    if let Some(label) = nonempty(&label) {
        if label != cluster {
            header.push_str(&format!("  [alias: {label}]"));
        }
    }
    let mut lines = vec![header];
    for (fpath, layer) in &member_rows {
        let layer_display = nonempty(layer).unwrap_or("Core");
        lines.push(format!("  - {fpath}  (layer={layer_display})"));
        if with_summary {
            let summary = get_latest_summary(&db, "file", fpath);
            match summary_short(&summary) {
                Some(short) => lines.push(format!("      short: {short}")),
                None => lines.push("      short: (no summary available)".to_string()),
            }
        }
    }
    lines.join("\n")
}
