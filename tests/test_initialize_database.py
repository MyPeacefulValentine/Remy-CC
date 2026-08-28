"""initialize_database fail-closed dispatch (R4.3).

The Python migration ladder is retired: every non-current schema version is
refused with the database preserved byte-for-byte at the logical level, and
the error points at `remy-daemon scan` as the schema owner.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "remy-index"))

from migrations import initialize_database
from schema import VERSION
from schema_snapshots import _make_v6_db, _make_v7_db, _make_v10_db

SNAPSHOT_FACTORIES = {
    "6.0.0": _make_v6_db,
    "7.0.0": _make_v7_db,
    "10.0.0": _make_v10_db,
}


def _dump(path):
    db = sqlite3.connect(str(path))
    try:
        return "\n".join(db.iterdump())
    finally:
        db.close()


@pytest.mark.parametrize("version", sorted(SNAPSHOT_FACTORIES))
def test_non_current_version_is_refused_and_preserved(tmp_path, version):
    db_path = tmp_path / "logic_index.db"
    SNAPSHOT_FACTORIES[version](db_path).close()
    before = _dump(db_path)

    with pytest.raises(RuntimeError, match="remy-daemon scan"):
        initialize_database(str(tmp_path), str(db_path))

    assert _dump(db_path) == before


def test_versionless_database_with_tables_is_refused(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    before = _dump(db_path)

    with pytest.raises(RuntimeError, match="no schema version"):
        initialize_database(str(tmp_path), str(db_path))

    assert _dump(db_path) == before


def test_fresh_database_initializes_at_current_version(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = initialize_database(str(tmp_path), str(db_path))
    try:
        row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    finally:
        db.close()
    assert row[0] == VERSION


def test_current_version_database_opens_without_error(tmp_path):
    db_path = tmp_path / "logic_index.db"
    initialize_database(str(tmp_path), str(db_path)).close()
    db = initialize_database(str(tmp_path), str(db_path))
    try:
        row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    finally:
        db.close()
    assert row[0] == VERSION
