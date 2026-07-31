"""Current retrieval projection derived from code facts and summary history."""

import hashlib
import json
from datetime import datetime


AVAILABLE_SUMMARY_STATUSES = frozenset(("ok", "oversized_warn"))
SKIPPABLE_SUMMARY_STATUSES = frozenset(
    ("pending", "corrupt", "oversized_hard")
)
SUMMARY_BARRIER_STATUS = "stale"

RETRIEVAL_SCHEMA_SQL = """
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
"""

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS retrieval_documents (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_kind "
    "ON retrieval_documents(node_kind, node_ref)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_file "
    "ON retrieval_documents(file_path)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
        name, name_tokens, signature, file_path, summary_short, summary_full,
        content='retrieval_documents', content_rowid='doc_id',
        tokenize='unicode61'
    )""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_fts_ai
    AFTER INSERT ON retrieval_documents BEGIN
        INSERT INTO retrieval_fts(
            rowid, name, name_tokens, signature, file_path,
            summary_short, summary_full
        ) VALUES (
            NEW.doc_id, NEW.name, NEW.name_tokens, NEW.signature, NEW.file_path,
            NEW.summary_short, NEW.summary_full
        );
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_fts_ad
    AFTER DELETE ON retrieval_documents BEGIN
        INSERT INTO retrieval_fts(
            retrieval_fts, rowid, name, name_tokens, signature, file_path,
            summary_short, summary_full
        ) VALUES (
            'delete', OLD.doc_id, OLD.name, OLD.name_tokens, OLD.signature,
            OLD.file_path, OLD.summary_short, OLD.summary_full
        );
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_fts_au
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
    END""",
)

_HASH_FIELDS = (
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
    "source_version",
)


def create_projection_schema(db):
    for statement in _SCHEMA_STATEMENTS:
        db.execute(statement)


def _parse_summary(summary_json):
    if not summary_json:
        return None
    try:
        payload = json.loads(summary_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def select_current_summary(db, node_kind, node_ref):
    rows = db.execute(
        "SELECT id, version, summary, status FROM summary_versions "
        "WHERE node_kind = ? AND node_ref = ? ORDER BY version DESC",
        (node_kind, node_ref),
    ).fetchall()
    latest_status = rows[0][3] if rows else None
    for summary_id, version, summary_json, status in rows:
        if status == SUMMARY_BARRIER_STATUS:
            return {
                "id": None,
                "version": None,
                "short": None,
                "full": None,
                "status": SUMMARY_BARRIER_STATUS,
                "latest_status": latest_status,
            }
        if status in SKIPPABLE_SUMMARY_STATUSES:
            continue
        if status not in AVAILABLE_SUMMARY_STATUSES:
            continue
        payload = _parse_summary(summary_json)
        if payload is None:
            continue
        short = payload.get("short")
        if not isinstance(short, str) or not short.strip():
            continue
        full = payload.get("full")
        if full is not None and not isinstance(full, str):
            full = None
        return {
            "id": summary_id,
            "version": version,
            "short": short,
            "full": full,
            "status": status,
            "latest_status": latest_status,
        }
    return {
        "id": None,
        "version": None,
        "short": None,
        "full": None,
        "status": latest_status,
        "latest_status": latest_status,
    }


def has_current_summary(db, node_kind, node_ref):
    return select_current_summary(db, node_kind, node_ref)["id"] is not None


def _document_hash(document):
    payload = {key: document.get(key) for key in _HASH_FIELDS}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_fact_document(db, node_kind, node_ref):
    if node_kind == "symbol":
        row = db.execute(
            "SELECT s.file_path, s.name, s.name_tokens, s.args, s.type, "
            "f.language FROM symbols s JOIN files f ON f.path = s.file_path "
            "WHERE s.file_path || '::' || s.name = ?",
            (node_ref,),
        ).fetchone()
        if row is None:
            return None
        file_path, name, name_tokens, signature, symbol_type, language = row
        return {
            "node_kind": "symbol",
            "node_ref": node_ref,
            "language": language,
            "symbol_type": symbol_type,
            "file_path": file_path,
            "name": name,
            "name_tokens": name_tokens,
            "signature": signature,
        }
    if node_kind == "file":
        row = db.execute(
            "SELECT path, language FROM files WHERE path = ?", (node_ref,)
        ).fetchone()
        if row is None:
            return None
        file_path, language = row
        return {
            "node_kind": "file",
            "node_ref": node_ref,
            "language": language,
            "symbol_type": None,
            "file_path": file_path,
            "name": file_path.rsplit("/", 1)[-1],
            "name_tokens": file_path.replace("/", " ").replace("_", " "),
            "signature": None,
        }
    if node_kind == "cluster":
        row = db.execute(
            "SELECT name, label FROM clusters WHERE name = ?", (node_ref,)
        ).fetchone()
        if row is None:
            return None
        name, label = row
        return {
            "node_kind": "cluster",
            "node_ref": node_ref,
            "language": None,
            "symbol_type": None,
            "file_path": None,
            "name": label or name,
            "name_tokens": name.replace("/", " ").replace("_", " "),
            "signature": None,
        }
    return None


def refresh_node(db, node_kind, node_ref):
    document = _load_fact_document(db, node_kind, node_ref)
    if document is None:
        delete_node(db, node_kind, node_ref)
        return None
    summary = select_current_summary(db, node_kind, node_ref)
    document["summary_short"] = summary["short"]
    document["summary_full"] = summary["full"]
    document["source_version"] = summary["version"]
    document["content_hash"] = _document_hash(document)
    document["updated_at"] = datetime.now().isoformat(timespec="seconds")
    values = (
        document["node_kind"],
        document["node_ref"],
        document["language"],
        document["symbol_type"],
        document["file_path"],
        document["name"],
        document["name_tokens"],
        document["signature"],
        document["summary_short"],
        document["summary_full"],
        document["content_hash"],
        document["source_version"],
        document["updated_at"],
    )
    cursor = db.execute(
        "UPDATE retrieval_documents SET language=?, symbol_type=?, file_path=?, "
        "name=?, name_tokens=?, signature=?, summary_short=?, summary_full=?, "
        "content_hash=?, source_version=?, updated_at=? "
        "WHERE node_kind=? AND node_ref=?",
        values[2:] + values[:2],
    )
    if cursor.rowcount == 0:
        db.execute(
            "INSERT INTO retrieval_documents ("
            "node_kind, node_ref, language, symbol_type, file_path, name, "
            "name_tokens, signature, summary_short, summary_full, content_hash, "
            "source_version, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    return document


def delete_node(db, node_kind, node_ref):
    db.execute(
        "DELETE FROM retrieval_documents WHERE node_kind = ? AND node_ref = ?",
        (node_kind, node_ref),
    )


def delete_file_nodes(db, file_path, symbol_refs=()):
    for node_ref in symbol_refs:
        delete_node(db, "symbol", node_ref)
    delete_node(db, "file", file_path)


def mark_current_summary_stale(db, node_kind, node_ref):
    current = select_current_summary(db, node_kind, node_ref)
    summary_id = current.get("id")
    if summary_id is None:
        refresh_node(db, node_kind, node_ref)
        return False
    db.execute(
        "UPDATE summary_versions SET status = 'stale' WHERE id = ?", (summary_id,)
    )
    refresh_node(db, node_kind, node_ref)
    return True


def mark_node_and_ancestors_stale(db, node_kind, node_ref):
    changed = mark_current_summary_stale(db, node_kind, node_ref)
    if node_kind == "symbol":
        if "::" not in node_ref:
            return changed
        file_ref = node_ref.rsplit("::", 1)[0]
        changed = mark_current_summary_stale(db, "file", file_ref) or changed
        row = db.execute(
            "SELECT c.name FROM clusters c JOIN cluster_members cm "
            "ON cm.cluster_id = c.id WHERE cm.file_path = ?",
            (file_ref,),
        ).fetchone()
        if row:
            changed = mark_current_summary_stale(db, "cluster", row[0]) or changed
    elif node_kind == "file":
        row = db.execute(
            "SELECT c.name FROM clusters c JOIN cluster_members cm "
            "ON cm.cluster_id = c.id WHERE cm.file_path = ?",
            (node_ref,),
        ).fetchone()
        if row:
            changed = mark_current_summary_stale(db, "cluster", row[0]) or changed
    return changed


def _table_exists(db, table_name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None


def rebuild_projection(db):
    create_projection_schema(db)
    db.execute("DELETE FROM retrieval_documents")
    symbol_refs = []
    if _table_exists(db, "symbols"):
        symbol_refs = [
            row[0]
            for row in db.execute(
                "SELECT file_path || '::' || name FROM symbols ORDER BY file_path, name"
            ).fetchall()
        ]
    file_refs = []
    if _table_exists(db, "files"):
        file_refs = [
            row[0] for row in db.execute("SELECT path FROM files ORDER BY path")
        ]
    cluster_refs = []
    if _table_exists(db, "clusters"):
        cluster_refs = [
            row[0] for row in db.execute("SELECT name FROM clusters ORDER BY name")
        ]
    for node_ref in symbol_refs:
        refresh_node(db, "symbol", node_ref)
    for node_ref in file_refs:
        refresh_node(db, "file", node_ref)
    for node_ref in cluster_refs:
        refresh_node(db, "cluster", node_ref)
    return validate_projection(db)


def validate_projection(db):
    expected = {
        "symbol": (
            db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            if _table_exists(db, "symbols") else 0
        ),
        "file": (
            db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            if _table_exists(db, "files") else 0
        ),
        "cluster": (
            db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
            if _table_exists(db, "clusters") else 0
        ),
    }
    actual = dict(
        db.execute(
            "SELECT node_kind, COUNT(*) FROM retrieval_documents GROUP BY node_kind"
        ).fetchall()
    )
    for node_kind, count in expected.items():
        if actual.get(node_kind, 0) != count:
            raise ValueError(
                "Retrieval projection count mismatch for {}: expected {}, got {}".format(
                    node_kind, count, actual.get(node_kind, 0)
                )
            )
    duplicates = db.execute(
        "SELECT COUNT(*) FROM (SELECT node_kind, node_ref, COUNT(*) AS n "
        "FROM retrieval_documents GROUP BY node_kind, node_ref HAVING n > 1)"
    ).fetchone()[0]
    if duplicates:
        raise ValueError("Retrieval projection contains duplicate nodes")
    return expected


def protected_summary_ids(db):
    protected = set()
    rows = db.execute(
        "SELECT node_kind, node_ref, MAX(id) FROM summary_versions "
        "GROUP BY node_kind, node_ref"
    ).fetchall()
    for node_kind, node_ref, latest_id in rows:
        protected.add(latest_id)
        current = select_current_summary(db, node_kind, node_ref)
        if current.get("id") is not None:
            protected.add(current["id"])
    return protected
