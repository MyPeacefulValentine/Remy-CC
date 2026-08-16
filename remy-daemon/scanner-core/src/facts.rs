//! Self-contained per-file fact value types (Send) flowing from parse
//! workers to the single writer thread.

/// parsers/base.py SymbolInfo.
#[derive(Debug, Clone, PartialEq)]
pub struct SymbolInfo {
    pub name: String,
    pub args: String,
    pub sym_type: String,
    pub lineno: i64,
    pub source_segment: String,
    pub end_lineno: Option<i64>,
    pub docstring: Option<String>,
    pub bases: Option<Vec<String>>,
}

/// parsers/base.py EdgeInfo — C/C++ direct edges only carry the defaults
/// (provenance/synthesized_from/via = NULL, call_form = "name").
#[derive(Debug, Clone, PartialEq)]
pub struct EdgeInfo {
    pub caller: String,
    pub callee: String,
    pub line: i64,
}

/// One extract_patterns dict; metadata is pre-encoded with the Python
/// default json.dumps format so the writer stores bytes directly.
#[derive(Debug, Clone, PartialEq)]
pub struct PatternFact {
    pub pattern_type: String,
    pub signal_name: Option<String>,
    pub handler: Option<String>,
    pub line: Option<i64>,
    pub metadata_json: Option<String>,
}

/// Parser cache identity as stored in the files table
/// (parsers/base.py ParserCacheIdentity.as_db_tuple).
#[derive(Debug, Clone, PartialEq)]
pub struct CacheIdentity {
    pub contract_version: String,
    pub backend: String,
    pub environment: String,
}

/// Everything scan_file writes for one source file.
#[derive(Debug, Clone)]
pub struct FileFacts {
    pub rel_path: String,
    pub struct_hash: String,
    pub language_id: String,
    pub layer: String,
    /// files.imports column, already json.dumps-default encoded.
    pub imports_json: String,
    /// files.import_bindings column (C/C++ has no name-binding imports: "[]").
    pub import_bindings_json: String,
    pub identity: CacheIdentity,
    pub canonical_symbols: Vec<SymbolInfo>,
    pub occurrences: Vec<OccurrenceRow>,
    pub edges: Vec<EdgeRow>,
    pub patterns: Vec<PatternFact>,
}

/// One symbol_occurrences row (selection already applied).
#[derive(Debug, Clone)]
pub struct OccurrenceRow {
    pub symbol: SymbolInfo,
    pub occurrence_index: i64,
    pub is_canonical: bool,
    pub conflict_kind: String,
    pub selection_reason: String,
    pub hash: String,
}

/// One deduplicated edges row (scan_file's seen_edges output).
#[derive(Debug, Clone, PartialEq)]
pub struct EdgeRow {
    pub caller: String,
    pub callee: String,
    pub line: i64,
    pub call_form: String,
}

/// Worker outcome for one file: facts, or a stage error that skips the file
/// without aborting the scan (struct_scan StageError semantics).
#[derive(Debug, Clone)]
pub enum FileOutcome {
    Facts(Box<FileFacts>),
    Failed { rel_path: String, message: String },
}
