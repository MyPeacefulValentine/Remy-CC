//! StructScanner._run_postprocess replication: direct-edge resolution
//! reset, import-binding derivation, three-tier call-edge disambiguation,
//! inferred-edge purge, Rust trait bases overwrite, then synthesis, file
//! kinds, and cluster detection. run() keeps the oracle's global recompute
//! (full-scan path); run_incremental() (F.1) serves scan_files and must
//! produce byte-identical VIEWS state.

use crate::rconfig::PostprocessConfig;
use crate::{clusters, pyjson, synth};
use rusqlite::{params, params_from_iter, Connection, OptionalExtension, Transaction};
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet};

/// SQLite bind-parameter budget per statement for id-list chunking.
const ID_CHUNK: usize = 500;

/// `sys.stdlib_module_names` of the pinned oracle interpreter
/// (CPython 3.12.9); the Python side reads it at runtime, so this constant
/// is bound to the oracle environment, not the host interpreter.
const PY_STDLIB_MODULE_NAMES: &[&str] = &[
    "__future__",
    "_abc",
    "_aix_support",
    "_ast",
    "_asyncio",
    "_bisect",
    "_blake2",
    "_bz2",
    "_codecs",
    "_codecs_cn",
    "_codecs_hk",
    "_codecs_iso2022",
    "_codecs_jp",
    "_codecs_kr",
    "_codecs_tw",
    "_collections",
    "_collections_abc",
    "_compat_pickle",
    "_compression",
    "_contextvars",
    "_crypt",
    "_csv",
    "_ctypes",
    "_curses",
    "_curses_panel",
    "_datetime",
    "_dbm",
    "_decimal",
    "_elementtree",
    "_frozen_importlib",
    "_frozen_importlib_external",
    "_functools",
    "_gdbm",
    "_hashlib",
    "_heapq",
    "_imp",
    "_io",
    "_json",
    "_locale",
    "_lsprof",
    "_lzma",
    "_markupbase",
    "_md5",
    "_msi",
    "_multibytecodec",
    "_multiprocessing",
    "_opcode",
    "_operator",
    "_osx_support",
    "_overlapped",
    "_pickle",
    "_posixshmem",
    "_posixsubprocess",
    "_py_abc",
    "_pydatetime",
    "_pydecimal",
    "_pyio",
    "_pylong",
    "_queue",
    "_random",
    "_scproxy",
    "_sha1",
    "_sha2",
    "_sha3",
    "_signal",
    "_sitebuiltins",
    "_socket",
    "_sqlite3",
    "_sre",
    "_ssl",
    "_stat",
    "_statistics",
    "_string",
    "_strptime",
    "_struct",
    "_symtable",
    "_thread",
    "_threading_local",
    "_tkinter",
    "_tokenize",
    "_tracemalloc",
    "_typing",
    "_uuid",
    "_warnings",
    "_weakref",
    "_weakrefset",
    "_winapi",
    "_wmi",
    "_zoneinfo",
    "abc",
    "aifc",
    "antigravity",
    "argparse",
    "array",
    "ast",
    "asyncio",
    "atexit",
    "audioop",
    "base64",
    "bdb",
    "binascii",
    "bisect",
    "builtins",
    "bz2",
    "cProfile",
    "calendar",
    "cgi",
    "cgitb",
    "chunk",
    "cmath",
    "cmd",
    "code",
    "codecs",
    "codeop",
    "collections",
    "colorsys",
    "compileall",
    "concurrent",
    "configparser",
    "contextlib",
    "contextvars",
    "copy",
    "copyreg",
    "crypt",
    "csv",
    "ctypes",
    "curses",
    "dataclasses",
    "datetime",
    "dbm",
    "decimal",
    "difflib",
    "dis",
    "doctest",
    "email",
    "encodings",
    "ensurepip",
    "enum",
    "errno",
    "faulthandler",
    "fcntl",
    "filecmp",
    "fileinput",
    "fnmatch",
    "fractions",
    "ftplib",
    "functools",
    "gc",
    "genericpath",
    "getopt",
    "getpass",
    "gettext",
    "glob",
    "graphlib",
    "grp",
    "gzip",
    "hashlib",
    "heapq",
    "hmac",
    "html",
    "http",
    "idlelib",
    "imaplib",
    "imghdr",
    "importlib",
    "inspect",
    "io",
    "ipaddress",
    "itertools",
    "json",
    "keyword",
    "lib2to3",
    "linecache",
    "locale",
    "logging",
    "lzma",
    "mailbox",
    "mailcap",
    "marshal",
    "math",
    "mimetypes",
    "mmap",
    "modulefinder",
    "msilib",
    "msvcrt",
    "multiprocessing",
    "netrc",
    "nis",
    "nntplib",
    "nt",
    "ntpath",
    "nturl2path",
    "numbers",
    "opcode",
    "operator",
    "optparse",
    "os",
    "ossaudiodev",
    "pathlib",
    "pdb",
    "pickle",
    "pickletools",
    "pipes",
    "pkgutil",
    "platform",
    "plistlib",
    "poplib",
    "posix",
    "posixpath",
    "pprint",
    "profile",
    "pstats",
    "pty",
    "pwd",
    "py_compile",
    "pyclbr",
    "pydoc",
    "pydoc_data",
    "pyexpat",
    "queue",
    "quopri",
    "random",
    "re",
    "readline",
    "reprlib",
    "resource",
    "rlcompleter",
    "runpy",
    "sched",
    "secrets",
    "select",
    "selectors",
    "shelve",
    "shlex",
    "shutil",
    "signal",
    "site",
    "smtplib",
    "sndhdr",
    "socket",
    "socketserver",
    "spwd",
    "sqlite3",
    "sre_compile",
    "sre_constants",
    "sre_parse",
    "ssl",
    "stat",
    "statistics",
    "string",
    "stringprep",
    "struct",
    "subprocess",
    "sunau",
    "symtable",
    "sys",
    "sysconfig",
    "syslog",
    "tabnanny",
    "tarfile",
    "telnetlib",
    "tempfile",
    "termios",
    "textwrap",
    "this",
    "threading",
    "time",
    "timeit",
    "tkinter",
    "token",
    "tokenize",
    "tomllib",
    "trace",
    "traceback",
    "tracemalloc",
    "tty",
    "turtle",
    "turtledemo",
    "types",
    "typing",
    "unicodedata",
    "unittest",
    "urllib",
    "uu",
    "uuid",
    "venv",
    "warnings",
    "wave",
    "weakref",
    "webbrowser",
    "winreg",
    "winsound",
    "wsgiref",
    "xdrlib",
    "xml",
    "xmlrpc",
    "zipapp",
    "zipfile",
    "zipimport",
    "zlib",
    "zoneinfo",
];

pub fn run(tx: &Transaction, config: &PostprocessConfig) -> rusqlite::Result<()> {
    reset_direct_edge_resolution(tx)?;
    resolve_call_edges(tx, config)?;
    purge_heuristic_edges(tx)?;
    overwrite_rust_trait_bases(tx)?;
    synth::run_all(tx, config)?;
    clusters::compute_file_kinds(tx, config)?;
    clusters::detect_clusters(tx, config)?;
    Ok(())
}

/// Per-scan change set aggregated by scan_files from writer::FileDelta.
#[derive(Debug, Default)]
pub struct ScanDelta {
    /// Written-and-changed plus deleted rel paths.
    pub touched_files: Vec<String>,
    /// Old ∪ new symbol names and short names of the touched files.
    pub names: HashSet<String>,
    pub added_py: Vec<String>,
    pub removed_py: Vec<String>,
}

/// (source_file, callee_file, callee_qualified) → count. The qualified
/// column must stay in the key: detect_clusters' entry symbols group by
/// callee_qualified, which pair-level counts cannot see change.
type InferredSnapshot = HashMap<(String, String, String), i64>;

fn inferred_snapshot(tx: &Transaction) -> rusqlite::Result<InferredSnapshot> {
    let mut stmt = tx.prepare(
        "SELECT source_file, COALESCE(callee_file, ''), \
         COALESCE(callee_qualified, ''), COUNT(*) FROM edges \
         WHERE provenance = 'inferred' GROUP BY 1, 2, 3",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(((row.get(0)?, row.get(1)?, row.get(2)?), row.get(3)?))
    })?;
    rows.collect()
}

/// Files whose import-binding resolution can flip when the indexed .py
/// set changes: every hit-count transition of derive_import_bindings'
/// unique-suffix rule involves a changed path matching a binding's forms.
fn import_binding_hosts(
    tx: &Transaction,
    changed: &[&String],
) -> rusqlite::Result<HashSet<String>> {
    let mut hosts = HashSet::new();
    if changed.is_empty() {
        return Ok(hosts);
    }
    let mut stmt = tx.prepare(
        "SELECT path, import_bindings FROM files \
         WHERE import_bindings IS NOT NULL AND import_bindings != '[]' \
         ORDER BY path",
    )?;
    let rows: Vec<(String, String)> = stmt
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    for (source_file, bindings_json) in rows {
        let bindings: Vec<Value> = serde_json::from_str::<Value>(&bindings_json)
            .ok()
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default();
        let affected = bindings.iter().any(|binding| {
            let module_name = binding.get("module").and_then(Value::as_str).unwrap_or("");
            if module_name.is_empty() {
                return false;
            }
            let head = module_name.split('.').next().unwrap_or("");
            if PY_STDLIB_MODULE_NAMES.contains(&head) {
                return false;
            }
            let joined = module_name.replace('.', "/");
            let suffixes = [format!("{joined}.py"), format!("{joined}/__init__.py")];
            changed.iter().any(|path| {
                suffixes
                    .iter()
                    .any(|suffix| *path == suffix || path.ends_with(&format!("/{suffix}")))
            })
        });
        if affected {
            hosts.insert(source_file);
        }
    }
    Ok(hosts)
}

/// F.1 targeted incremental postprocess. Contract: VIEWS state must be
/// byte-identical to run() — reset set is a conservative superset of the
/// re-resolvable direct edges, purge/synth/trait-bases stay global, and
/// the snapshot diff feeds every inferred-edge change into kind/cluster.
pub fn run_incremental(
    tx: &Transaction,
    config: &PostprocessConfig,
    delta: &ScanDelta,
) -> rusqlite::Result<()> {
    let touched: HashSet<&str> = delta.touched_files.iter().map(String::as_str).collect();
    let changed_py: Vec<&String> = delta
        .added_py
        .iter()
        .chain(delta.removed_py.iter())
        .collect();
    let import_hosts = import_binding_hosts(tx, &changed_py)?;

    let mut affected_files: BTreeSet<String> = delta.touched_files.iter().cloned().collect();
    let mut target_ids: Vec<i64> = Vec::new();
    {
        // Single full-table pass: edges.callee carries no index.
        let mut stmt = tx.prepare(
            "SELECT id, source_file, callee, callee_file FROM edges \
             WHERE provenance != 'inferred' OR provenance IS NULL",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
            ))
        })?;
        for row in rows {
            let (id, source_file, callee, callee_file) = row?;
            if touched.contains(source_file.as_str())
                || import_hosts.contains(&source_file)
                || delta.names.contains(&callee)
            {
                target_ids.push(id);
                if let Some(old_callee_file) = callee_file {
                    affected_files.insert(old_callee_file);
                }
                affected_files.insert(source_file);
            }
        }
    }

    for chunk in target_ids.chunks(ID_CHUNK) {
        let placeholders = vec!["?"; chunk.len()].join(",");
        tx.execute(
            &format!("DELETE FROM edge_candidates WHERE edge_id IN ({placeholders})"),
            params_from_iter(chunk.iter()),
        )?;
        tx.execute(
            &format!(
                "UPDATE edges SET callee_qualified = NULL, callee_file = NULL, \
                 provenance = NULL WHERE id IN ({placeholders})"
            ),
            params_from_iter(chunk.iter()),
        )?;
    }

    let target_id_set: HashSet<i64> = target_ids.iter().copied().collect();
    resolve_call_edges_filtered(tx, config, Some(&target_id_set))?;

    for chunk in target_ids.chunks(ID_CHUNK) {
        let placeholders = vec!["?"; chunk.len()].join(",");
        let mut stmt = tx.prepare(&format!(
            "SELECT callee_file FROM edges WHERE callee_file IS NOT NULL \
             AND id IN ({placeholders})"
        ))?;
        let rows = stmt.query_map(params_from_iter(chunk.iter()), |row| {
            row.get::<_, String>(0)
        })?;
        for row in rows {
            affected_files.insert(row?);
        }
    }

    let before = inferred_snapshot(tx)?;
    purge_heuristic_edges(tx)?;
    overwrite_rust_trait_bases(tx)?;
    synth::run_all(tx, config)?;
    let after = inferred_snapshot(tx)?;
    for key in before.keys().chain(after.keys()) {
        if before.get(key) != after.get(key) {
            let (source_file, callee_file, _qualified) = key;
            affected_files.insert(source_file.clone());
            if !callee_file.is_empty() {
                affected_files.insert(callee_file.clone());
            }
        }
    }

    clusters::compute_file_kinds_for(tx, config, &affected_files)?;
    let groups: BTreeSet<String> = affected_files
        .iter()
        .map(|path| clusters::top_group(path).to_string())
        .collect();
    clusters::detect_clusters_for(tx, config, &groups)?;
    Ok(())
}

fn reset_direct_edge_resolution(tx: &Transaction) -> rusqlite::Result<()> {
    tx.execute("DELETE FROM edge_candidates", [])?;
    tx.execute(
        "UPDATE edges SET callee_qualified = NULL, callee_file = NULL, \
         provenance = NULL WHERE provenance != 'inferred' OR provenance IS NULL",
        [],
    )?;
    Ok(())
}

fn purge_heuristic_edges(tx: &Transaction) -> rusqlite::Result<()> {
    tx.execute("DELETE FROM edges WHERE provenance = 'inferred'", [])?;
    Ok(())
}

pub struct ImportDerivation {
    pub supplements: HashMap<String, Vec<String>>,
    pub externals: HashMap<String, HashSet<String>>,
}

/// `StructScanner._derive_import_bindings`: unique path-suffix matching
/// against indexed Python files with a stdlib short circuit. Exported for
/// the MCP query_dependencies query-time derivation (single semantic source
/// shared with scan-time edge resolution).
pub fn derive_import_bindings(conn: &Connection) -> rusqlite::Result<ImportDerivation> {
    let mut stmt = conn.prepare("SELECT path, import_bindings FROM files ORDER BY path")?;
    let rows: Vec<(String, Option<String>)> = stmt
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    let py_paths: Vec<&str> = rows
        .iter()
        .filter(|(path, _)| path.ends_with(".py"))
        .map(|(path, _)| path.as_str())
        .collect();

    let suffix_hits = |module_name: &str| -> Vec<String> {
        let joined = module_name.replace('.', "/");
        let suffixes = [format!("{joined}.py"), format!("{joined}/__init__.py")];
        let mut hits = Vec::new();
        for path in &py_paths {
            for suffix in &suffixes {
                if *path == suffix.as_str() || path.ends_with(&format!("/{suffix}")) {
                    hits.push((*path).to_string());
                    break;
                }
            }
        }
        hits
    };

    let mut derivation = ImportDerivation {
        supplements: HashMap::new(),
        externals: HashMap::new(),
    };
    for (source_file, bindings_json) in &rows {
        let bindings: Vec<Value> = bindings_json
            .as_deref()
            .and_then(|text| serde_json::from_str::<Value>(text).ok())
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default();
        for binding in bindings {
            let module_name = binding
                .get("module")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let names: Vec<String> = binding
                .get("names")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default();
            if module_name.is_empty() {
                continue;
            }
            let head = module_name.split('.').next().unwrap_or("");
            if PY_STDLIB_MODULE_NAMES.contains(&head) {
                derivation
                    .externals
                    .entry(source_file.clone())
                    .or_default()
                    .extend(names);
                continue;
            }
            let hits = suffix_hits(&module_name);
            if hits.len() == 1 {
                derivation
                    .supplements
                    .entry(source_file.clone())
                    .or_default()
                    .push(hits.into_iter().next().unwrap());
            } else if hits.is_empty() {
                derivation
                    .externals
                    .entry(source_file.clone())
                    .or_default()
                    .extend(names);
            }
        }
    }
    Ok(derivation)
}

/// `StructScanner._resolve_call_edges`: same-file > direct-import > global
/// scoring with speculative downgrades for ties and attribute calls. The
/// global tier only matches candidates whose files.language equals the
/// caller's (the index does not resolve cross-language FFI).
fn resolve_call_edges(tx: &Transaction, config: &PostprocessConfig) -> rusqlite::Result<()> {
    resolve_call_edges_filtered(tx, config, None)
}

/// `only`: restrict to these edge ids. Unresolvable edges stay NULL
/// permanently, so the incremental path must not re-walk that backlog;
/// per-edge work is independent, so filtering preserves equivalence.
fn resolve_call_edges_filtered(
    tx: &Transaction,
    config: &PostprocessConfig,
    only: Option<&HashSet<i64>>,
) -> rusqlite::Result<()> {
    let fanout_cap = config.resolve_fanout_cap;
    let score_same = config.resolve_score_same_file;
    let score_import = config.resolve_score_direct_import;
    let score_global = config.resolve_score_global;

    let derivation = derive_import_bindings(tx)?;

    let unresolved: Vec<(i64, String, String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT id, source_file, callee, call_form FROM edges \
             WHERE callee_qualified IS NULL \
             AND (provenance != 'inferred' OR provenance IS NULL) \
             ORDER BY source_file, caller, callee, COALESCE(line, 0), id",
        )?;
        let collected = stmt
            .query_map([], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?
            .collect::<Result<_, _>>()?;
        collected
    };

    for (edge_id, source_file, callee_name, call_form) in unresolved {
        if only.is_some_and(|ids| !ids.contains(&edge_id)) {
            continue;
        }
        let file_row: Option<(Option<String>, Option<String>)> = tx
            .query_row(
                "SELECT imports, language FROM files WHERE path = ?1",
                params![source_file],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let (imports_json, source_language) = file_row.unwrap_or((None, None));
        let mut import_list: Vec<String> = imports_json
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .and_then(|value| value.as_array().cloned())
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default();
        if let Some(extras) = derivation.supplements.get(&source_file) {
            for extra in extras {
                if !import_list.contains(extra) {
                    import_list.push(extra.clone());
                }
            }
        }

        let mut candidates: Vec<(String, i64)> = Vec::new();

        let same_file: Vec<String> = {
            let mut stmt = tx.prepare(
                "SELECT file_path || '::' || name FROM symbols \
                 WHERE file_path = ?1 AND (name = ?2 OR short_name = ?2) \
                 ORDER BY file_path, name",
            )?;
            let collected = stmt
                .query_map(params![source_file, callee_name], |row| row.get(0))?
                .collect::<Result<_, _>>()?;
            collected
        };
        for qualified in same_file {
            candidates.push((qualified, score_same));
        }

        if !import_list.is_empty() {
            let placeholders = vec!["?"; import_list.len()].join(",");
            let sql = format!(
                "SELECT file_path || '::' || name FROM symbols \
                 WHERE file_path IN ({placeholders}) \
                 AND (name = ? OR short_name = ?) ORDER BY file_path, name"
            );
            let mut stmt = tx.prepare(&sql)?;
            let bound: Vec<&str> = import_list
                .iter()
                .map(String::as_str)
                .chain([callee_name.as_str(), callee_name.as_str()])
                .collect();
            let import_syms: Vec<String> = stmt
                .query_map(params_from_iter(bound), |row| row.get(0))?
                .collect::<Result<_, _>>()?;
            for qualified in import_syms {
                if !candidates
                    .iter()
                    .any(|(existing, _)| *existing == qualified)
                {
                    candidates.push((qualified, score_import));
                }
            }
        }

        if candidates.is_empty() {
            if call_form != "attribute"
                && derivation
                    .externals
                    .get(&source_file)
                    .is_some_and(|names| names.contains(&callee_name))
            {
                continue;
            }
            let mut stmt = tx.prepare(
                "SELECT symbols.file_path || '::' || symbols.name FROM symbols \
                 JOIN files ON files.path = symbols.file_path \
                 WHERE (symbols.name = ?1 OR symbols.short_name = ?1) \
                 AND symbols.file_path != ?2 AND files.language = ?3 \
                 ORDER BY symbols.file_path, symbols.name LIMIT ?4",
            )?;
            let global_syms: Vec<String> = stmt
                .query_map(
                    params![callee_name, source_file, source_language, fanout_cap],
                    |row| row.get(0),
                )?
                .collect::<Result<_, _>>()?;
            for qualified in global_syms {
                candidates.push((qualified, score_global));
            }
        }

        if candidates.is_empty() {
            continue;
        }

        candidates.sort_by(|left, right| right.1.cmp(&left.1).then_with(|| left.0.cmp(&right.0)));
        let best = candidates[0].0.clone();
        let best_file = best.split_once("::").map(|(file, _)| file.to_string());

        let tied = candidates.len() > 1 && candidates[0].1 == candidates[1].1;
        let provenance = if tied || (call_form == "attribute" && candidates[0].1 < score_same) {
            "speculative"
        } else if candidates[0].1 >= score_import {
            "definite"
        } else {
            "probable"
        };

        tx.execute(
            "UPDATE edges SET callee_qualified = ?1, callee_file = ?2, provenance = ?3 \
             WHERE id = ?4",
            params![best, best_file, provenance, edge_id],
        )?;

        if candidates.len() > 1 {
            for (qualified, score) in candidates.iter().take(fanout_cap.max(0) as usize) {
                tx.execute(
                    "INSERT OR IGNORE INTO edge_candidates (edge_id, candidate_qualified, score) \
                     VALUES (?1,?2,?3)",
                    params![edge_id, qualified, score],
                )?;
            }
        }
    }
    Ok(())
}

fn resolve_rust_impl_target(
    tx: &Transaction,
    impl_file: &str,
    full_type: &str,
) -> rusqlite::Result<Option<(String, String)>> {
    let exact: Vec<(String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, name FROM symbols WHERE file_path = ?1 AND name = ?2 \
             AND type IN ('struct', 'enum')",
        )?;
        let collected = stmt
            .query_map(params![impl_file, full_type], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })?
            .collect::<Result<_, _>>()?;
        collected
    };
    if exact.len() == 1 {
        return Ok(exact.into_iter().next());
    }
    let short = full_type.rsplit('.').next().unwrap_or(full_type);
    let same_file: Vec<(String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, name FROM symbols WHERE file_path = ?1 AND short_name = ?2 \
             AND type IN ('struct', 'enum') ORDER BY name",
        )?;
        let collected = stmt
            .query_map(params![impl_file, short], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })?
            .collect::<Result<_, _>>()?;
        collected
    };
    if same_file.len() == 1 {
        return Ok(same_file.into_iter().next());
    }
    if !same_file.is_empty() {
        return Ok(None);
    }
    let global_rows: Vec<(String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, name FROM symbols WHERE short_name = ?1 \
             AND type IN ('struct', 'enum') AND file_path LIKE '%.rs' \
             ORDER BY file_path, name",
        )?;
        let collected = stmt
            .query_map(params![short], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<Result<_, _>>()?;
        collected
    };
    if global_rows.len() == 1 {
        return Ok(global_rows.into_iter().next());
    }
    Ok(None)
}

/// `StructScanner._overwrite_rust_trait_bases`: full re-derivation of .rs
/// struct/enum bases from the global rust_trait_impl facts.
fn overwrite_rust_trait_bases(tx: &Transaction) -> rusqlite::Result<()> {
    let impls: Vec<(String, String, String)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, signal_name, handler FROM patterns \
             WHERE pattern_type = 'rust_trait_impl' \
             AND signal_name IS NOT NULL AND handler IS NOT NULL \
             ORDER BY file_path, COALESCE(line, 0), signal_name, handler",
        )?;
        let collected = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        collected
    };

    let mut merged: HashMap<(String, String), Vec<String>> = HashMap::new();
    for (impl_file, trait_name, full_type) in &impls {
        let Some(target) = resolve_rust_impl_target(tx, impl_file, full_type)? else {
            continue;
        };
        let traits = merged.entry(target).or_default();
        if !traits.contains(trait_name) {
            traits.push(trait_name.clone());
        }
    }

    let rust_types: Vec<(String, String, Option<String>)> = {
        let mut stmt = tx.prepare(
            "SELECT file_path, name, bases FROM symbols \
             WHERE type IN ('struct', 'enum') AND file_path LIKE '%.rs' \
             ORDER BY file_path, name",
        )?;
        let collected = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
            .collect::<Result<_, _>>()?;
        collected
    };
    for (file_path, name, old_bases) in rust_types {
        let new_bases = merged
            .get(&(file_path.clone(), name.clone()))
            .filter(|traits| !traits.is_empty())
            .map(|traits| {
                pyjson::dumps_default(&Value::Array(
                    traits.iter().map(|t| Value::String(t.clone())).collect(),
                ))
            });
        if new_bases != old_bases {
            tx.execute(
                "UPDATE symbols SET bases = ?1 WHERE file_path = ?2 AND name = ?3",
                params![new_bases, file_path, name],
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
            full_scan_timeout: 1800,
        }
    }

    fn insert_file(tx: &Transaction, path: &str, imports: &str, bindings: &str) {
        tx.execute(
            "INSERT INTO files (path, struct_hash, language, imports, import_bindings) \
             VALUES (?1, 'h', 'PythonParser', ?2, ?3)",
            params![path, imports, bindings],
        )
        .unwrap();
    }

    fn insert_symbol(tx: &Transaction, path: &str, name: &str, sym_type: &str) {
        let short = name.rsplit('.').next().unwrap_or(name);
        tx.execute(
            "INSERT INTO symbols (file_path, name, short_name, type, lineno, hash, name_tokens) \
             VALUES (?1, ?2, ?3, ?4, 1, 'x', '')",
            params![path, name, short, sym_type],
        )
        .unwrap();
    }

    #[test]
    fn three_tier_scoring_and_speculative_downgrades() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        insert_file(&tx, "a.py", r#"["b.py", "c.py"]"#, "[]");
        insert_file(&tx, "b.py", "[]", "[]");
        insert_file(&tx, "c.py", "[]", "[]");
        insert_symbol(&tx, "a.py", "local_fn", "function");
        insert_symbol(&tx, "b.py", "imported_fn", "function");
        insert_symbol(&tx, "b.py", "twin", "function");
        insert_symbol(&tx, "c.py", "twin", "function");
        for callee in ["local_fn", "imported_fn", "twin"] {
            tx.execute(
                "INSERT INTO edges (source_file, caller, callee, line, call_form) \
                 VALUES ('a.py', 'main', ?1, 1, 'name')",
                params![callee],
            )
            .unwrap();
        }
        resolve_call_edges(&tx, &config()).unwrap();
        let rows: Vec<(String, String, String)> = {
            let mut stmt = tx
                .prepare("SELECT callee, callee_qualified, provenance FROM edges ORDER BY callee")
                .unwrap();
            stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap()
        };
        assert_eq!(
            rows,
            vec![
                (
                    "imported_fn".to_string(),
                    "b.py::imported_fn".to_string(),
                    "definite".to_string()
                ),
                (
                    "local_fn".to_string(),
                    "a.py::local_fn".to_string(),
                    "definite".to_string()
                ),
                (
                    "twin".to_string(),
                    "b.py::twin".to_string(),
                    "speculative".to_string()
                ),
            ]
        );
        let candidate_count: i64 = tx
            .query_row("SELECT COUNT(*) FROM edge_candidates", [], |row| row.get(0))
            .unwrap();
        assert_eq!(candidate_count, 2);
    }

    #[test]
    fn import_bindings_supplement_and_external_suppression() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        insert_file(
            &tx,
            "app.py",
            "[]",
            r#"[{"module": "pkg.util", "names": ["helper"]}, {"module": "os", "names": ["getenv"]}, {"module": "vendor", "names": ["ext_fn"]}]"#,
        );
        insert_file(&tx, "pkg/util.py", "[]", "[]");
        insert_symbol(&tx, "pkg/util.py", "helper", "function");
        insert_file(&tx, "other.py", "[]", "[]");
        insert_symbol(&tx, "other.py", "ext_fn", "function");
        for callee in ["helper", "ext_fn"] {
            tx.execute(
                "INSERT INTO edges (source_file, caller, callee, line, call_form) \
                 VALUES ('app.py', 'main', ?1, 1, 'name')",
                params![callee],
            )
            .unwrap();
        }
        resolve_call_edges(&tx, &config()).unwrap();
        let helper: (Option<String>, Option<String>) = tx
            .query_row(
                "SELECT callee_qualified, provenance FROM edges WHERE callee='helper'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            helper,
            (
                Some("pkg/util.py::helper".to_string()),
                Some("definite".to_string())
            )
        );
        let ext: Option<String> = tx
            .query_row(
                "SELECT callee_qualified FROM edges WHERE callee='ext_fn'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(ext, None, "unresolved external names skip the global tier");
    }

    #[test]
    fn global_tier_is_language_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        insert_file(&tx, "a.py", "[]", "[]");
        tx.execute(
            "INSERT INTO files (path, struct_hash, language, imports, import_bindings) \
             VALUES ('native.rs', 'h', 'RustParser', '[]', '[]')",
            [],
        )
        .unwrap();
        insert_symbol(&tx, "native.rs", "cross_probe", "function");
        tx.execute(
            "INSERT INTO edges (source_file, caller, callee, line, call_form) \
             VALUES ('a.py', 'main', 'cross_probe', 1, 'name')",
            [],
        )
        .unwrap();
        resolve_call_edges(&tx, &config()).unwrap();
        let resolved: Option<String> = tx
            .query_row(
                "SELECT callee_qualified FROM edges WHERE callee='cross_probe'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            resolved, None,
            "global tier must not match cross-language symbols"
        );

        insert_file(&tx, "b.py", "[]", "[]");
        insert_symbol(&tx, "b.py", "cross_probe", "function");
        reset_direct_edge_resolution(&tx).unwrap();
        resolve_call_edges(&tx, &config()).unwrap();
        let row: (Option<String>, Option<String>) = tx
            .query_row(
                "SELECT callee_qualified, provenance FROM edges WHERE callee='cross_probe'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            row,
            (
                Some("b.py::cross_probe".to_string()),
                Some("probable".to_string())
            )
        );
    }

    #[test]
    fn rust_trait_bases_overwrite_is_idempotent_and_shrinks() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        insert_file(&tx, "traits.rs", "[]", "[]");
        insert_file(&tx, "backend.rs", "[]", "[]");
        insert_symbol(&tx, "traits.rs", "Store", "struct");
        tx.execute(
            "INSERT INTO patterns (file_path, pattern_type, signal_name, handler, line) \
             VALUES ('backend.rs', 'rust_trait_impl', 'Persist', 'Store', 1)",
            [],
        )
        .unwrap();
        overwrite_rust_trait_bases(&tx).unwrap();
        overwrite_rust_trait_bases(&tx).unwrap();
        let bases: Option<String> = tx
            .query_row("SELECT bases FROM symbols WHERE name='Store'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(bases.as_deref(), Some(r#"["Persist"]"#));

        tx.execute("DELETE FROM patterns", []).unwrap();
        overwrite_rust_trait_bases(&tx).unwrap();
        let cleared: Option<String> = tx
            .query_row("SELECT bases FROM symbols WHERE name='Store'", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(cleared, None);
    }

    #[test]
    fn ambiguous_impl_target_merges_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(&dir.path().join("db.sqlite")).unwrap().0;
        let tx = conn.transaction().unwrap();
        for path in ["dup_a.rs", "dup_b.rs", "main.rs"] {
            insert_file(&tx, path, "[]", "[]");
        }
        insert_symbol(&tx, "dup_a.rs", "Widget", "struct");
        insert_symbol(&tx, "dup_b.rs", "Widget", "struct");
        tx.execute(
            "INSERT INTO patterns (file_path, pattern_type, signal_name, handler, line) \
             VALUES ('main.rs', 'rust_trait_impl', 'Persist', 'Widget', 1)",
            [],
        )
        .unwrap();
        overwrite_rust_trait_bases(&tx).unwrap();
        let with_bases: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE bases IS NOT NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(with_bases, 0);
    }
}
