
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT,
    kind_hint TEXT,
    actual_kind TEXT,
    parser_contract_version TEXT NOT NULL DEFAULT '',
    parser_backend TEXT NOT NULL DEFAULT '',
    parser_environment TEXT NOT NULL DEFAULT '{}',
    import_bindings TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name TEXT NOT NULL,
    short_name TEXT,
    type TEXT NOT NULL,
    args TEXT,
    lineno INTEGER,
    end_lineno INTEGER,
    hash TEXT,
    bases TEXT,
    name_tokens TEXT NOT NULL DEFAULT '',
    UNIQUE(file_path, name)
);
CREATE TABLE IF NOT EXISTS symbol_occurrences (
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name TEXT NOT NULL,
    occurrence_index INTEGER NOT NULL,
    type TEXT NOT NULL,
    args TEXT,
    lineno INTEGER,
    end_lineno INTEGER,
    hash TEXT NOT NULL,
    is_canonical INTEGER NOT NULL CHECK (is_canonical IN (0, 1)),
    conflict_kind TEXT NOT NULL CHECK (
        conflict_kind IN ('type_variant', 'signature_variant', 'duplicate_definition')
    ),
    selection_reason TEXT NOT NULL,
    PRIMARY KEY (file_path, name, occurrence_index)
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    caller TEXT NOT NULL,
    callee TEXT NOT NULL,
    callee_file TEXT,
    callee_qualified TEXT,
    line INTEGER,
    provenance TEXT,
    synthesized_from TEXT,
    via TEXT,
    call_form TEXT NOT NULL DEFAULT 'name'
);
CREATE TABLE IF NOT EXISTS edge_candidates (
    edge_id INTEGER NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    candidate_qualified TEXT NOT NULL,
    score INTEGER DEFAULT 0,
    PRIMARY KEY (edge_id, candidate_qualified)
);
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    pattern_type TEXT NOT NULL,
    signal_name TEXT,
    handler TEXT,
    line INTEGER,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    label TEXT,
    entry_symbols TEXT NOT NULL,
    file_count INTEGER
);
CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, file_path)
);
CREATE TABLE IF NOT EXISTS summary_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_kind TEXT NOT NULL,
    node_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    decision_rationale TEXT,
    decision_dimension TEXT,
    decision_confidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(node_kind, node_ref, version)
);
CREATE TABLE IF NOT EXISTS node_change_counters (
    node_kind TEXT NOT NULL,
    node_ref TEXT NOT NULL,
    child_change_count INTEGER NOT NULL DEFAULT 0,
    leaf_descendant_count INTEGER NOT NULL DEFAULT 0,
    last_force_recompute_at TEXT,
    PRIMARY KEY (node_kind, node_ref)
);
CREATE TABLE IF NOT EXISTS judge_cache (
    payload_hash TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_short ON symbols(short_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_occurrences_file_name ON symbol_occurrences(file_path, name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_occurrences_one_canonical
ON symbol_occurrences(file_path, name) WHERE is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_edges_callee_q ON edges(callee_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(source_file, caller);
CREATE INDEX IF NOT EXISTS idx_edges_provenance ON edges(provenance);
CREATE INDEX IF NOT EXISTS idx_edges_source_file ON edges(source_file);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_inferred_identity
ON edges(source_file, caller, callee_qualified, via)
WHERE provenance = 'inferred' AND callee_qualified IS NOT NULL AND via IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_patterns_type_signal ON patterns(pattern_type, signal_name);
CREATE INDEX IF NOT EXISTS idx_patterns_file ON patterns(file_path);
CREATE INDEX IF NOT EXISTS idx_sv_lookup ON summary_versions(node_kind, node_ref, version DESC);
CREATE INDEX IF NOT EXISTS idx_sv_status ON summary_versions(status, node_kind);
CREATE INDEX IF NOT EXISTS idx_jc_created ON judge_cache(created_at);


CREATE TABLE IF NOT EXISTS retrieval_documents (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_kind TEXT NOT NULL,
    node_ref TEXT NOT NULL,
    language TEXT,
    symbol_type TEXT,
    file_path TEXT,
    name TEXT,
    name_tokens TEXT,
    signature TEXT,
    summary_short TEXT,
    summary_full TEXT,
    content_hash TEXT NOT NULL,
    source_version INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE(node_kind, node_ref)
);
CREATE INDEX IF NOT EXISTS idx_retrieval_kind
ON retrieval_documents(node_kind, node_ref);
CREATE INDEX IF NOT EXISTS idx_retrieval_file
ON retrieval_documents(file_path);
CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
    name,
    name_tokens,
    signature,
    file_path,
    summary_short,
    summary_full,
    content='retrieval_documents',
    content_rowid='doc_id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS retrieval_fts_ai
AFTER INSERT ON retrieval_documents BEGIN
    INSERT INTO retrieval_fts(
        rowid, name, name_tokens, signature, file_path,
        summary_short, summary_full
    ) VALUES (
        NEW.doc_id, NEW.name, NEW.name_tokens, NEW.signature, NEW.file_path,
        NEW.summary_short, NEW.summary_full
    );
END;
CREATE TRIGGER IF NOT EXISTS retrieval_fts_ad
AFTER DELETE ON retrieval_documents BEGIN
    INSERT INTO retrieval_fts(
        retrieval_fts, rowid, name, name_tokens, signature, file_path,
        summary_short, summary_full
    ) VALUES (
        'delete', OLD.doc_id, OLD.name, OLD.name_tokens, OLD.signature,
        OLD.file_path, OLD.summary_short, OLD.summary_full
    );
END;
CREATE TRIGGER IF NOT EXISTS retrieval_fts_au
AFTER UPDATE ON retrieval_documents BEGIN
    INSERT INTO retrieval_fts(
        retrieval_fts, rowid, name, name_tokens, signature, file_path,
        summary_short, summary_full
    ) VALUES (
        'delete', OLD.doc_id, OLD.name, OLD.name_tokens, OLD.signature,
        OLD.file_path, OLD.summary_short, OLD.summary_full
    );
    INSERT INTO retrieval_fts(
        rowid, name, name_tokens, signature, file_path,
        summary_short, summary_full
    ) VALUES (
        NEW.doc_id, NEW.name, NEW.name_tokens, NEW.signature, NEW.file_path,
        NEW.summary_short, NEW.summary_full
    );
END;
