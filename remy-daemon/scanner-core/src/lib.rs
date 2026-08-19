//! scanner-core — Rust replication of the frozen Python oracle: per-file
//! fact extraction for all four languages (R3.2/R3.3) plus the global
//! postprocess (R3.4: direct-edge disambiguation, import-binding
//! derivation, inferred-edge synthesis, file kinds, clusters, summary
//! invalidation, and the retrieval projection).
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

pub mod clusters;
pub mod config;
pub mod discovery;
pub mod facts;
pub mod fnmatch;
pub mod hashes;
pub mod language;
pub mod lock;
pub mod parse_c_cpp;
pub mod parse_python;
pub mod parse_rust;
pub mod parse_ts;
pub mod patterns_c;
pub mod postprocess;
pub mod projection;
pub mod py_repr;
pub mod py_unparse;
pub mod pyjson;
pub mod rconfig;
pub mod result;
pub mod scan;
pub mod selection;
pub mod synth;
pub mod writer;

/// Logic index DDL — the shared single source (skills/remy-index/schema.sql).
/// schema.py reads the same file; a Python-side contract test asserts the
/// two views never diverge.
pub const SCHEMA_SQL: &str = include_str!("../../../skills/remy-index/schema.sql");

/// Logic index schema version written to the meta table.
pub const SCHEMA_VERSION: &str = "12.0.0";
