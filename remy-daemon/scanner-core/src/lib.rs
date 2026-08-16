//! scanner-core — Rust replication of the frozen Python oracle's per-file
//! fact extraction (R3.2 scope: C/C++ only).
//!
//! Every byte written to the fact tables replicates the Python scanner's
//! output for the phase-1 per-file subset (files without kind columns,
//! symbols, symbol_occurrences, direct edges, patterns). Global
//! postprocessing (edge disambiguation, clusters, retrieval projection)
//! is out of scope until R3.4.
//!
//! Cross-implementation contracts replicated here:
//! - JSON column encoding: Python `json.dumps` default format
//!   (`", "`/`": "` separators, `ensure_ascii=True`) for
//!   files.imports / files.import_bindings / symbols.bases /
//!   patterns.metadata; compact sorted format (`","`/`":"`,
//!   `ensure_ascii=False`) for files.parser_environment.
//! - Hashes: symbol hash = MD5 of comment-stripped, whitespace-removed
//!   segment; struct hash = MD5 of the full decoded source.
//! - File decoding: UTF-8 strict with universal-newline translation
//!   (`\r\n` and lone `\r` become `\n`), BOM preserved.
//! - Exclusion matching: Python `fnmatch` semantics, case-insensitive on
//!   Windows.

pub mod config;
pub mod discovery;
pub mod facts;
pub mod fnmatch;
pub mod hashes;
pub mod language;
pub mod parse_c_cpp;
pub mod parse_python;
pub mod parse_ts;
pub mod patterns_c;
pub mod py_repr;
pub mod py_unparse;
pub mod pyjson;
pub mod result;
pub mod scan;
pub mod selection;
pub mod writer;

/// Logic index DDL — the shared single source (skills/remy-index/schema.sql).
/// schema.py reads the same file; a Python-side contract test asserts the
/// two views never diverge.
pub const SCHEMA_SQL: &str = include_str!("../../../skills/remy-index/schema.sql");

/// Logic index schema version written to the meta table.
pub const SCHEMA_VERSION: &str = "12.0.0";
