"""Rust schema-owner dispatch matrix, end to end through `remy-daemon scan`.

R4.2 ruling (docs/RETIREMENT.md §2.5): the Rust owner supports the current
schema version only. Below-current or versionless-with-tables databases are
backed up to `.bak` and rebuilt, with an incremental entry escalating to
the full file set inside the same call; at-current databases open without
a rebuild; newer or unparseable versions are refused unchanged. Requires a
built remy-daemon binary; every test skips when it is unavailable.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder_samples import _make_v6_db, _make_v7_db, _make_v10_db

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_CORPUS = REPO_ROOT / "oracle" / "fixtures" / "corpus" / "python"
TARGET_DIR = REPO_ROOT / "remy-daemon" / "target"

SAMPLE_FACTORIES = {
    "6.0.0": _make_v6_db,
    "7.0.0": _make_v7_db,
    "10.0.0": _make_v10_db,
}


def _daemon_binary() -> Path | None:
    name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    candidates = [TARGET_DIR / profile / name for profile in ("release", "debug")]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


BINARY = _daemon_binary()

pytestmark = pytest.mark.skipif(BINARY is None, reason="remy-daemon binary not built")


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    destination = tmp_path / "corpus"
    shutil.copytree(PY_CORPUS, destination)
    return destination


def _scan(root: Path, db: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(BINARY), "scan", "--root", str(root), "--db", str(db), "--result-json", *extra],
        capture_output=True,
        text=True,
    )


def _report(completed: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _db_version(path: Path) -> str:
    db = sqlite3.connect(str(path))
    try:
        return db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
    finally:
        db.close()


def _file_paths(path: Path) -> set[str]:
    db = sqlite3.connect(str(path))
    try:
        return {row[0] for row in db.execute("SELECT path FROM files")}
    finally:
        db.close()


@pytest.mark.parametrize("version", sorted(SAMPLE_FACTORIES))
def test_incremental_entry_rebuilds_below_current_and_escalates_to_full_set(
    py_project: Path, tmp_path: Path, version: str
):
    db_path = tmp_path / "logic_index.db"
    SAMPLE_FACTORIES[version](db_path).close()

    completed = _scan(py_project, db_path, "--files", "app.py")

    assert completed.returncode == 0, completed.stderr
    assert "SCHEMA_REBUILD" in completed.stderr
    report = _report(completed)
    assert report["outcome"] == "success"
    expected = {entry.name for entry in py_project.iterdir() if entry.suffix == ".py"}
    assert _file_paths(db_path) == expected
    assert _db_version(db_path) == "12.0.0"

    backup_path = Path(str(db_path) + ".bak")
    assert backup_path.exists()
    assert _db_version(backup_path) == version
    assert "src/foo.py" in _file_paths(backup_path)


def test_versionless_database_with_tables_is_rebuilt(py_project: Path, tmp_path: Path):
    db_path = tmp_path / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL)")
    db.execute("INSERT INTO files VALUES ('legacy.py', 'h0')")
    db.commit()
    db.close()

    completed = _scan(py_project, db_path, "--files", "app.py")

    assert completed.returncode == 0, completed.stderr
    assert _db_version(db_path) == "12.0.0"
    backup_path = Path(str(db_path) + ".bak")
    assert "legacy.py" in _file_paths(backup_path)


@pytest.mark.parametrize("version", ["999.0.0", "not-a-version"])
def test_newer_or_unparseable_version_is_refused_unchanged(
    py_project: Path, tmp_path: Path, version: str
):
    db_path = tmp_path / "logic_index.db"
    db = _make_v6_db(db_path)
    db.execute("UPDATE meta SET value=? WHERE key='version'", (version,))
    db.commit()
    db.close()
    before = db_path.read_bytes()

    completed = _scan(py_project, db_path, "--files", "app.py")

    assert completed.returncode == 1
    report = _report(completed)
    assert report["outcome"] == "failed"
    assert version in report["errors"][0]["message"]
    assert db_path.read_bytes() == before
    assert not Path(str(db_path) + ".bak").exists()


def test_empty_database_file_is_initialized_without_backup(py_project: Path, tmp_path: Path):
    db_path = tmp_path / "logic_index.db"
    db_path.touch()

    completed = _scan(py_project, db_path, "--files", "app.py")

    assert completed.returncode == 0, completed.stderr
    assert "SCHEMA_REBUILD" not in completed.stderr
    assert _db_version(db_path) == "12.0.0"
    assert not Path(str(db_path) + ".bak").exists()


def test_current_version_database_opens_without_rebuild(py_project: Path, tmp_path: Path):
    db_path = tmp_path / "logic_index.db"
    first = _scan(py_project, db_path)
    assert first.returncode == 0, first.stderr
    files_before = _file_paths(db_path)

    second = _scan(py_project, db_path, "--files", "app.py")

    assert second.returncode == 0, second.stderr
    assert "SCHEMA_REBUILD" not in second.stderr
    assert not Path(str(db_path) + ".bak").exists()
    assert _file_paths(db_path) == files_before
    assert _db_version(db_path) == "12.0.0"


def test_rebuild_backup_includes_committed_wal_rows(py_project: Path, tmp_path: Path):
    db_path = tmp_path / "logic_index.db"
    _make_v10_db(db_path).close()
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("INSERT INTO files (path, struct_hash) VALUES ('wal.py', 'wal-hash')")
    db.commit()
    db.close()

    completed = _scan(py_project, db_path, "--files", "app.py")

    assert completed.returncode == 0, completed.stderr
    backup_path = Path(str(db_path) + ".bak")
    assert "wal.py" in _file_paths(backup_path)
