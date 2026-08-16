//! Scan orchestration: discovery → parallel parse workers → bounded
//! channel → single writer thread owning the connection, one transaction
//! per scan (R3.2 decision: parse pool + mpsc + single writer; full scans
//! write tables first and rebuild indexes afterwards).

use crate::config::ScanConfig;
use crate::discovery::{self, DiscoveredFile};
use crate::facts::{EdgeRow, FileFacts, FileOutcome, OccurrenceRow};
use crate::hashes;
use crate::language::Language;
use crate::result::{RunStatus, ScanResult, StageError};
use crate::selection;
use crate::writer;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};

pub fn default_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}

/// Parse one source file into its per-file facts (StructScanner.scan_file
/// up to, but excluding, the DB writes).
pub fn parse_one(
    root_dir: &Path,
    config: &ScanConfig,
    full_path: &Path,
    rel_path: &str,
) -> FileOutcome {
    let bytes = match std::fs::read(full_path) {
        Ok(bytes) => bytes,
        Err(_) => {
            return FileOutcome::Failed {
                rel_path: rel_path.to_string(),
                message: format!("Unable to read source file: {rel_path}"),
            }
        }
    };
    let source = match hashes::decode_source(&bytes) {
        Ok(source) => source,
        Err(_) => {
            return FileOutcome::Failed {
                rel_path: rel_path.to_string(),
                message: format!("Unable to read source file: {rel_path}"),
            }
        }
    };

    let struct_hash = hashes::struct_hash(&source);
    let file_path_str = full_path.to_string_lossy();
    let file_name = full_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let Some(language) = Language::resolve(&file_name) else {
        return FileOutcome::Failed {
            rel_path: rel_path.to_string(),
            message: format!("No parser handles source file: {rel_path}"),
        };
    };
    let parsed = language.parse_file(&source, full_path, &file_path_str, root_dir);
    let selection = selection::select_symbols(parsed.symbols);
    let layer = config.match_file_to_layer(rel_path);

    let imports_json = crate::pyjson::dumps_default(&serde_json::Value::Array(
        parsed
            .imports
            .into_iter()
            .map(serde_json::Value::String)
            .collect(),
    ));

    let occurrences: Vec<OccurrenceRow> = selection
        .occurrences
        .into_iter()
        .map(|occurrence| {
            let hash =
                hashes::symbol_hash(&language.symbol_hash_input(&occurrence.symbol.source_segment));
            OccurrenceRow {
                symbol: occurrence.symbol,
                occurrence_index: occurrence.occurrence_index,
                is_canonical: occurrence.is_canonical,
                conflict_kind: occurrence.conflict_kind,
                selection_reason: occurrence.selection_reason,
                hash,
            }
        })
        .collect();

    // scan_file's seen_edges dedup: (caller, callee) keyed, minimum line,
    // call_form locks to "name" once any duplicate reports "name".
    let mut edge_index: HashMap<(String, String), usize> = HashMap::new();
    let mut edges: Vec<EdgeRow> = Vec::new();
    for edge in parsed.edges {
        let key = (edge.caller.clone(), edge.callee.clone());
        if let Some(&i) = edge_index.get(&key) {
            if edge.line < edges[i].line {
                edges[i].line = edge.line;
            }
            if edge.call_form == "name" {
                edges[i].call_form = "name".to_string();
            }
            continue;
        }
        edge_index.insert(key, edges.len());
        edges.push(EdgeRow {
            caller: edge.caller,
            callee: edge.callee,
            line: edge.line,
            call_form: edge.call_form.to_string(),
        });
    }

    FileOutcome::Facts(Box::new(FileFacts {
        rel_path: rel_path.to_string(),
        struct_hash,
        language_id: language.language_id().to_string(),
        layer,
        imports_json,
        import_bindings_json: parsed.import_bindings_json,
        identity: parsed.identity,
        canonical_symbols: selection.canonical_symbols,
        occurrences,
        edges,
        patterns: parsed.patterns,
    }))
}

struct ParsePool {
    receiver: mpsc::Receiver<FileOutcome>,
    handles: Vec<std::thread::JoinHandle<()>>,
}

fn spawn_parse_pool(
    root_dir: &Path,
    config: &ScanConfig,
    files: Vec<DiscoveredFile>,
    jobs: usize,
) -> ParsePool {
    let jobs = jobs.max(1);
    let (sender, receiver) = mpsc::sync_channel::<FileOutcome>(2 * jobs);
    let queue = Arc::new(Mutex::new(files));
    let root: Arc<PathBuf> = Arc::new(root_dir.to_path_buf());
    let config = Arc::new(config.clone());

    let mut handles = Vec::with_capacity(jobs);
    for _ in 0..jobs {
        let sender = sender.clone();
        let queue = Arc::clone(&queue);
        let root = Arc::clone(&root);
        let config = Arc::clone(&config);
        handles.push(std::thread::spawn(move || loop {
            let task = { queue.lock().expect("parse queue poisoned").pop() };
            let Some(file) = task else { break };
            let outcome = parse_one(&root, &config, &file.full_path, &file.rel_path);
            if sender.send(outcome).is_err() {
                break;
            }
        }));
    }
    drop(sender);
    ParsePool { receiver, handles }
}

fn join_pool(handles: Vec<std::thread::JoinHandle<()>>) -> Result<(), String> {
    let mut panicked = false;
    for handle in handles {
        if handle.join().is_err() {
            panicked = true;
        }
    }
    if panicked {
        Err("parse worker panicked; scan transaction rolled back".to_string())
    } else {
        Ok(())
    }
}

/// Full scan: discover, parse in parallel, write everything in one
/// transaction with indexes rebuilt after the bulk load. `progress`
/// emits a PROGRESS line to stderr every 250 written files (throughput
/// curve measurements).
pub fn scan_full(
    root_dir: &Path,
    db_path: &Path,
    jobs: usize,
    progress: bool,
) -> Result<ScanResult, String> {
    let config = ScanConfig::load(root_dir);
    let files =
        discovery::discover(root_dir, &config).map_err(|e| format!("discovery failed: {e}"))?;
    let discovered: Vec<String> = files.iter().map(|f| f.rel_path.clone()).collect();

    let mut conn = writer::open_db(db_path).map_err(|e| format!("open db failed: {e}"))?;
    let pool = spawn_parse_pool(root_dir, &config, files, jobs);

    let started = std::time::Instant::now();
    let mut written: u64 = 0;
    let mut successful = Vec::new();
    let mut failed = Vec::new();
    let mut errors = Vec::new();
    {
        let tx = conn
            .transaction()
            .map_err(|e| format!("begin failed: {e}"))?;
        writer::drop_secondary_indexes(&tx).map_err(|e| format!("index drop failed: {e}"))?;
        for outcome in pool.receiver.iter() {
            match outcome {
                FileOutcome::Facts(facts) => {
                    writer::write_file_facts(&tx, &facts)
                        .map_err(|e| format!("write failed for {}: {e}", facts.rel_path))?;
                    successful.push(facts.rel_path.clone());
                }
                FileOutcome::Failed { rel_path, message } => {
                    errors.push(StageError::new(
                        "file_scan",
                        message,
                        Some(rel_path.clone()),
                    ));
                    failed.push(rel_path);
                }
            }
            written += 1;
            if progress && written.is_multiple_of(250) {
                eprintln!(
                    "PROGRESS files={} elapsed_ms={}",
                    written,
                    started.elapsed().as_millis()
                );
            }
        }
        join_pool(pool.handles)?;
        if progress {
            eprintln!(
                "PROGRESS files={} elapsed_ms={} stage=parse_done",
                written,
                started.elapsed().as_millis()
            );
        }
        writer::rebuild_indexes(&tx).map_err(|e| format!("index rebuild failed: {e}"))?;
        writer::write_meta(&tx, Some(discovered.len()))
            .map_err(|e| format!("meta write failed: {e}"))?;
        tx.commit().map_err(|e| format!("commit failed: {e}"))?;
    }

    Ok(ScanResult::from_parts(
        discovered,
        successful,
        failed,
        Vec::new(),
        errors,
        true,
    ))
}

/// Incremental scan of explicit paths (struct_scan --files semantics for
/// an isolated DB): excluded paths are acknowledged untouched, missing
/// files delete their rows, everything happens in one transaction.
pub fn scan_files(
    root_dir: &Path,
    db_path: &Path,
    file_paths: &[String],
    jobs: usize,
) -> Result<ScanResult, String> {
    let config = ScanConfig::load(root_dir);

    let mut requested: Vec<(String, PathBuf)> = Vec::new();
    for file_path in file_paths {
        let raw = Path::new(file_path);
        let full_path = if raw.is_absolute() {
            raw.to_path_buf()
        } else {
            root_dir.join(raw)
        };
        let rel = discovery::rel_path_slash(&full_path, root_dir);
        requested.push((rel, full_path));
    }

    let mut discovered = Vec::new();
    let mut successful = Vec::new();
    let mut failed = Vec::new();
    let mut deleted = Vec::new();
    let mut errors = Vec::new();

    let mut to_parse = Vec::new();
    let mut to_delete = Vec::new();
    for (rel, full_path) in requested {
        if config.is_path_excluded(&rel) {
            successful.push(rel);
            continue;
        }
        discovered.push(rel.clone());
        if !full_path.exists() {
            to_delete.push(rel);
            continue;
        }
        let file_name = full_path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        if Language::resolve(&file_name).is_none() {
            successful.push(rel);
            continue;
        }
        to_parse.push(DiscoveredFile {
            full_path,
            rel_path: rel,
        });
    }

    let mut conn = writer::open_db(db_path).map_err(|e| format!("open db failed: {e}"))?;
    let pool = spawn_parse_pool(root_dir, &config, to_parse, jobs);
    {
        let tx = conn
            .transaction()
            .map_err(|e| format!("begin failed: {e}"))?;
        for rel in to_delete {
            if writer::delete_file(&tx, &rel).map_err(|e| format!("delete failed: {e}"))? {
                deleted.push(rel.clone());
            }
            successful.push(rel);
        }
        for outcome in pool.receiver.iter() {
            match outcome {
                FileOutcome::Facts(facts) => {
                    writer::write_file_facts(&tx, &facts)
                        .map_err(|e| format!("write failed for {}: {e}", facts.rel_path))?;
                    successful.push(facts.rel_path.clone());
                }
                FileOutcome::Failed { rel_path, message } => {
                    errors.push(StageError::new(
                        "file_scan",
                        message,
                        Some(rel_path.clone()),
                    ));
                    failed.push(rel_path);
                }
            }
        }
        join_pool(pool.handles)?;
        writer::write_meta(&tx, None).map_err(|e| format!("meta write failed: {e}"))?;
        tx.commit().map_err(|e| format!("commit failed: {e}"))?;
    }

    Ok(ScanResult::from_parts(
        discovered, successful, failed, deleted, errors, true,
    ))
}

/// CLI entry shared by `remy-daemon scan`.
pub struct ScanArgs {
    pub root: PathBuf,
    pub db: PathBuf,
    pub files: Vec<String>,
    pub jobs: Option<usize>,
    pub result_json: bool,
    pub progress: bool,
}

pub fn run_scan(args: &ScanArgs) -> u8 {
    let jobs = args.jobs.unwrap_or_else(default_jobs);
    let outcome = if args.files.is_empty() {
        scan_full(&args.root, &args.db, jobs, args.progress)
    } else {
        scan_files(&args.root, &args.db, &args.files, jobs)
    };
    match outcome {
        Ok(result) => {
            if args.result_json {
                println!("{}", result.to_json_line());
            } else {
                for error in &result.errors {
                    let location = error
                        .path
                        .as_deref()
                        .map(|p| format!(" ({p})"))
                        .unwrap_or_default();
                    eprintln!("[{}]{} {}", error.stage, location, error.message);
                }
                println!(
                    "STRUCT_SCAN_RESULT status={} successful={} failed={}",
                    result.status.value(),
                    result.successful_paths.len(),
                    result.failed_paths.len()
                );
            }
            result.status.exit_code()
        }
        Err(message) => {
            if args.result_json {
                let result = ScanResult::from_parts(
                    Vec::new(),
                    Vec::new(),
                    args.files.clone(),
                    Vec::new(),
                    vec![StageError::new("worker", message, None)],
                    false,
                );
                println!("{}", result.to_json_line());
            } else {
                eprintln!("Structural scan failed: {message}");
            }
            RunStatus::Failed.exit_code()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn write_corpus(root: &Path) {
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::write(
            root.join("src/util.h"),
            "#ifndef UTIL_H\n#define UTIL_H\nint add(int a, int b);\n#endif\n",
        )
        .unwrap();
        std::fs::write(
            root.join("src/util.c"),
            "#include \"util.h\"\n\n/** Adds. */\nint add(int a, int b) { return a + b; }\n\nint twice(int x) { return add(x, x); }\n",
        )
        .unwrap();
    }

    fn phase1_projection(db_path: &Path) -> Vec<Vec<String>> {
        let conn = Connection::open(db_path).unwrap();
        let mut out = Vec::new();
        for sql in [
            "SELECT path, struct_hash, language, layer, imports, parser_contract_version, import_bindings FROM files ORDER BY path",
            "SELECT file_path, name, short_name, type, args, lineno, end_lineno, hash, COALESCE(bases,''), name_tokens FROM symbols ORDER BY file_path, name",
            "SELECT file_path, name, occurrence_index, type, hash, is_canonical, conflict_kind, selection_reason FROM symbol_occurrences ORDER BY file_path, name, occurrence_index",
            "SELECT source_file, caller, callee, line, call_form FROM edges ORDER BY source_file, caller, callee, line",
            "SELECT file_path, pattern_type, COALESCE(signal_name,''), COALESCE(handler,''), COALESCE(line,-1), COALESCE(metadata,'') FROM patterns ORDER BY file_path, pattern_type, signal_name, handler, line, metadata",
        ] {
            let mut stmt = conn.prepare(sql).unwrap();
            let column_count = stmt.column_count();
            let rows = stmt
                .query_map([], |row| {
                    let mut values = Vec::new();
                    for i in 0..column_count {
                        let value: rusqlite::types::Value = row.get(i)?;
                        values.push(format!("{value:?}"));
                    }
                    Ok(values)
                })
                .unwrap();
            for row in rows {
                out.push(row.unwrap());
            }
        }
        out
    }

    #[test]
    fn full_scan_writes_expected_facts() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_corpus(root);
        let db_path = root.join("state.db");
        let result = scan_full(root, &db_path, 1, false).unwrap();
        assert_eq!(result.status, RunStatus::Success);
        assert_eq!(result.successful_paths, vec!["src/util.c", "src/util.h"]);

        let conn = Connection::open(&db_path).unwrap();
        let imports: String = conn
            .query_row(
                "SELECT imports FROM files WHERE path='src/util.c'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(imports, r#"["src/util.h"]"#);
        let edge: (String, String, i64, String) = conn
            .query_row(
                "SELECT caller, callee, line, call_form FROM edges WHERE source_file='src/util.c'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(
            edge,
            (
                "twice".to_string(),
                "add".to_string(),
                6,
                "name".to_string()
            )
        );
        let doc_symbol: String = conn
            .query_row(
                "SELECT name_tokens FROM symbols WHERE file_path='src/util.c' AND name='add'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(doc_symbol, "add");
    }

    #[test]
    fn jobs_commute_on_phase1_projection() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_corpus(root);
        for i in 0..12 {
            std::fs::write(
                root.join(format!("src/gen{i}.c")),
                format!("int gen{i}(void) {{ return {i}; }}\n"),
            )
            .unwrap();
        }
        let db1 = root.join("one.db");
        let db2 = root.join("two.db");
        let db8 = root.join("eight.db");
        scan_full(root, &db1, 1, false).unwrap();
        scan_full(root, &db2, 2, false).unwrap();
        scan_full(root, &db8, 8, false).unwrap();
        let p1 = phase1_projection(&db1);
        assert!(!p1.is_empty());
        assert_eq!(p1, phase1_projection(&db2));
        assert_eq!(p1, phase1_projection(&db8));
    }

    #[test]
    fn incremental_equals_full_scan() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_corpus(root);
        let full_db = root.join("full.db");
        let inc_db = root.join("inc.db");
        scan_full(root, &full_db, 2, false).unwrap();
        for rel in ["src/util.c", "src/util.h"] {
            let result = scan_files(root, &inc_db, &[rel.to_string()], 1).unwrap();
            assert_eq!(result.status, RunStatus::Success);
        }
        assert_eq!(phase1_projection(&full_db), phase1_projection(&inc_db));
    }

    #[test]
    fn incremental_deletes_missing_files() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_corpus(root);
        let db = root.join("state.db");
        scan_full(root, &db, 1, false).unwrap();
        std::fs::remove_file(root.join("src/util.h")).unwrap();
        let result = scan_files(root, &db, &["src/util.h".to_string()], 1).unwrap();
        assert_eq!(result.deleted_paths, vec!["src/util.h"]);
        let conn = Connection::open(&db).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM files WHERE path='src/util.h'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn unreadable_file_is_stage_error_not_abort() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write_corpus(root);
        std::fs::write(root.join("src/bad.c"), [0xff, 0xfe, 0x00, 0x41]).unwrap();
        let db = root.join("state.db");
        let result = scan_full(root, &db, 2, false).unwrap();
        assert_eq!(result.status, RunStatus::Partial);
        assert_eq!(result.failed_paths, vec!["src/bad.c"]);
        assert_eq!(result.errors.len(), 1);
        assert_eq!(result.errors[0].stage, "file_scan");
        assert_eq!(result.successful_paths.len(), 2);
    }
}
