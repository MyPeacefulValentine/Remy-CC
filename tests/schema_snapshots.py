"""Frozen pre-current sample databases (v6 / v7 / v10).

Generated once from the live migration handlers and verified equivalent by
normalized iterdump at freeze time (sample-source replacement,
docs/RETIREMENT.md §8 ruling 6, anchor `a474e5f`). Virtual tables are created
natively with external-content FTS rebuilt, because iterdump's
writable_schema replay does not run under executescript. Each factory returns
an open sqlite3 connection at the named schema version.
"""

import sqlite3

V6_SNAPSHOT = """
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name TEXT NOT NULL,
    short_name TEXT,
    type TEXT NOT NULL,
    args TEXT,
    lineno INTEGER,
    end_lineno INTEGER,
    hash TEXT,
    summary TEXT,
    bases TEXT,
    name_tokens TEXT NOT NULL DEFAULT '',
    UNIQUE(file_path, name)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE symbols_fts USING fts5(
    name, name_tokens, file_path, summary,
    content='symbols', content_rowid='id', tokenize='unicode61'
);
INSERT INTO "files" VALUES('src/foo.py','h1','PythonParser','Core','[]');
INSERT INTO "symbols" VALUES(1,'src/foo.py','alpha','alpha','function','()',1,5,'sh1','Computes alpha',NULL,'alpha');
INSERT INTO "meta" VALUES('version','6.0.0');
INSERT INTO "symbols_fts"("symbols_fts") VALUES('rebuild');
CREATE TRIGGER symbols_fts_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
    VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
END;
CREATE TRIGGER symbols_fts_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, name_tokens, file_path, summary)
    VALUES ('delete', OLD.id, OLD.name, OLD.name_tokens, OLD.file_path, OLD.summary);
END;
CREATE TRIGGER symbols_fts_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, name_tokens, file_path, summary)
    VALUES ('delete', OLD.id, OLD.name, OLD.name_tokens, OLD.file_path, OLD.summary);
    INSERT INTO symbols_fts(rowid, name, name_tokens, file_path, summary)
    VALUES (NEW.id, NEW.name, NEW.name_tokens, NEW.file_path, NEW.summary);
END;
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('symbols',1);
"""

V7_SNAPSHOT = """
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT
, kind_hint TEXT, actual_kind TEXT);
CREATE TABLE symbols (
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
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE summary_versions (
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
CREATE TABLE node_change_counters (
                node_kind TEXT NOT NULL,
                node_ref TEXT NOT NULL,
                child_change_count INTEGER NOT NULL DEFAULT 0,
                leaf_descendant_count INTEGER NOT NULL DEFAULT 0,
                last_force_recompute_at TEXT,
                PRIMARY KEY (node_kind, node_ref)
            );
CREATE TABLE judge_cache (
                payload_hash TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
CREATE TABLE migration_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_version TEXT NOT NULL,
                to_version TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
CREATE VIRTUAL TABLE summary_fts USING fts5(
                node_kind UNINDEXED,
                node_ref UNINDEXED,
                short,
                full,
                tokenize='unicode61'
            );
INSERT INTO "files" VALUES('src/foo.py','h1','PythonParser','Core','[]',NULL,NULL);
INSERT INTO "symbols" VALUES(1,'src/foo.py','alpha','alpha','function','()',1,5,'sh1',NULL,'alpha');
INSERT INTO "meta" VALUES('version','7.0.0');
INSERT INTO "summary_versions" VALUES(1,'symbol','src/foo.py::alpha',1,'{"short": "Computes alpha", "full": null}','ok',NULL,NULL,NULL,'2026-08-28T20:34:46');
INSERT INTO "migration_log" VALUES(1,'6.0.0','7.0.0','2026-08-28T20:34:46');
INSERT INTO "summary_fts" VALUES('symbol','src/foo.py::alpha','Computes alpha','');
CREATE INDEX idx_sv_lookup ON summary_versions(node_kind, node_ref, version DESC);
CREATE INDEX idx_sv_status ON summary_versions(status, node_kind);
CREATE INDEX idx_jc_created ON judge_cache(created_at);
CREATE TRIGGER summary_fts_ai AFTER INSERT ON summary_versions
            WHEN NEW.status NOT IN ('pending', 'corrupt') BEGIN
                INSERT INTO summary_fts(rowid, node_kind, node_ref, short, full)
                VALUES (NEW.id, NEW.node_kind, NEW.node_ref,
                        COALESCE(json_extract(NEW.summary, '$.short'), ''),
                        COALESCE(json_extract(NEW.summary, '$.full'), ''));
            END;
CREATE TRIGGER summary_fts_ad AFTER DELETE ON summary_versions BEGIN
                DELETE FROM summary_fts WHERE rowid = OLD.id;
            END;
CREATE TRIGGER summary_fts_au AFTER UPDATE ON summary_versions BEGIN
                DELETE FROM summary_fts WHERE rowid = OLD.id;
                INSERT INTO summary_fts(rowid, node_kind, node_ref, short, full)
                SELECT NEW.id, NEW.node_kind, NEW.node_ref,
                       COALESCE(json_extract(NEW.summary, '$.short'), ''),
                       COALESCE(json_extract(NEW.summary, '$.full'), '')
                WHERE NEW.status NOT IN ('pending', 'corrupt');
            END;
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('symbols',1);
INSERT INTO sqlite_sequence VALUES('summary_versions',1);
INSERT INTO sqlite_sequence VALUES('migration_log',1);
"""

V10_SNAPSHOT = """
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    struct_hash TEXT NOT NULL,
    language TEXT,
    layer TEXT DEFAULT 'Core',
    imports TEXT
, kind_hint TEXT, actual_kind TEXT);
CREATE TABLE symbols (
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
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE summary_versions (
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
CREATE TABLE node_change_counters (
                node_kind TEXT NOT NULL,
                node_ref TEXT NOT NULL,
                child_change_count INTEGER NOT NULL DEFAULT 0,
                leaf_descendant_count INTEGER NOT NULL DEFAULT 0,
                last_force_recompute_at TEXT,
                PRIMARY KEY (node_kind, node_ref)
            );
CREATE TABLE judge_cache (
                payload_hash TEXT PRIMARY KEY,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
CREATE TABLE migration_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_version TEXT NOT NULL,
                to_version TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
CREATE TABLE symbol_occurrences (
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
CREATE TABLE retrieval_documents (
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
CREATE VIRTUAL TABLE retrieval_fts USING fts5(
        name, name_tokens, signature, file_path, summary_short, summary_full,
        content='retrieval_documents', content_rowid='doc_id',
        tokenize='unicode61'
    );
CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            caller TEXT NOT NULL,
            callee TEXT NOT NULL,
            callee_file TEXT,
            callee_qualified TEXT,
            line INTEGER,
            provenance TEXT,
            synthesized_from TEXT,
            via TEXT
        );
INSERT INTO "files" VALUES('src/foo.py','h1','PythonParser','Core','[]',NULL,NULL);
INSERT INTO "symbols" VALUES(1,'src/foo.py','alpha','alpha','function','()',1,5,'sh1',NULL,'alpha');
INSERT INTO "meta" VALUES('version','10.0.0');
INSERT INTO "summary_versions" VALUES(1,'symbol','src/foo.py::alpha',1,'{"short": "Computes alpha", "full": null}','ok',NULL,NULL,NULL,'2026-08-28T20:34:46');
INSERT INTO "migration_log" VALUES(1,'6.0.0','7.0.0','2026-08-28T20:34:46');
INSERT INTO "migration_log" VALUES(2,'7.0.0','8.0.0','2026-08-28T20:34:46');
INSERT INTO "migration_log" VALUES(3,'8.0.0','9.0.0','2026-08-28T20:34:46');
INSERT INTO "migration_log" VALUES(4,'9.0.0','10.0.0','2026-08-28T20:34:46');
INSERT INTO "retrieval_documents" VALUES(3,'symbol','src/foo.py::alpha','PythonParser','function','src/foo.py','alpha','alpha','()','Computes alpha',NULL,'6081dd145cb0a274e43b3449f41479a779e75abeacf8567a99ce845f35532bde',1,'2026-08-28T20:34:46');
INSERT INTO "retrieval_documents" VALUES(4,'file','src/foo.py','PythonParser',NULL,'src/foo.py','foo.py','src foo.py',NULL,NULL,NULL,'e1bc6b40bdd819478940e7b97b5b2acd462d8c9a1ac33c22747ec413be1505b2',NULL,'2026-08-28T20:34:46');
INSERT INTO "retrieval_fts"("retrieval_fts") VALUES('rebuild');
CREATE INDEX idx_sv_lookup ON summary_versions(node_kind, node_ref, version DESC);
CREATE INDEX idx_sv_status ON summary_versions(status, node_kind);
CREATE INDEX idx_jc_created ON judge_cache(created_at);
CREATE INDEX idx_occurrences_file_name ON symbol_occurrences(file_path, name);
CREATE UNIQUE INDEX idx_occurrences_one_canonical ON symbol_occurrences(file_path, name) WHERE is_canonical = 1;
CREATE INDEX idx_retrieval_kind ON retrieval_documents(node_kind, node_ref);
CREATE INDEX idx_retrieval_file ON retrieval_documents(file_path);
CREATE TRIGGER retrieval_fts_ai
    AFTER INSERT ON retrieval_documents BEGIN
        INSERT INTO retrieval_fts(
            rowid, name, name_tokens, signature, file_path,
            summary_short, summary_full
        ) VALUES (
            NEW.doc_id, NEW.name, NEW.name_tokens, NEW.signature, NEW.file_path,
            NEW.summary_short, NEW.summary_full
        );
    END;
CREATE TRIGGER retrieval_fts_ad
    AFTER DELETE ON retrieval_documents BEGIN
        INSERT INTO retrieval_fts(
            retrieval_fts, rowid, name, name_tokens, signature, file_path,
            summary_short, summary_full
        ) VALUES (
            'delete', OLD.doc_id, OLD.name, OLD.name_tokens, OLD.signature,
            OLD.file_path, OLD.summary_short, OLD.summary_full
        );
    END;
CREATE TRIGGER retrieval_fts_au
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
CREATE UNIQUE INDEX idx_edges_inferred_identity ON edges(source_file, caller, callee_qualified, via) WHERE provenance = 'inferred' AND callee_qualified IS NOT NULL AND via IS NOT NULL;
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('symbols',1);
INSERT INTO sqlite_sequence VALUES('summary_versions',1);
INSERT INTO sqlite_sequence VALUES('migration_log',4);
INSERT INTO sqlite_sequence VALUES('retrieval_documents',4);
"""


def _build(path, script):
    db = sqlite3.connect(str(path))
    db.executescript(script)
    db.commit()
    return db


def _make_v6_db(path):
    return _build(path, V6_SNAPSHOT)


def _make_v7_db(path):
    return _build(path, V7_SNAPSHOT)


def _make_v10_db(path):
    return _build(path, V10_SNAPSHOT)
