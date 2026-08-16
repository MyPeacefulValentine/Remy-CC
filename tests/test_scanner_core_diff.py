"""Cross-implementation diff tests: Rust scanner-core vs the Python oracle.

Requires a built remy-daemon binary (cargo build --workspace); every test
skips when the binary or the tree-sitter backend is unavailable, so plain
Python CI legs are unaffected.
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

from oracle import classification as oracle_classification
from oracle import comparator as oracle_comparator
from oracle import normalization as oracle_normalization

REPO_ROOT = Path(__file__).resolve().parents[1]
C_CORPUS = REPO_ROOT / "oracle" / "fixtures" / "corpus" / "c"
TARGET_DIR = REPO_ROOT / "remy-daemon" / "target"

try:
    import parsers.c_cpp_parser as c_cpp_parser

    TREE_SITTER_AVAILABLE = c_cpp_parser.TREE_SITTER_AVAILABLE
except Exception:  # pragma: no cover - import environment issues equal skip
    TREE_SITTER_AVAILABLE = False


def _daemon_binary() -> Path | None:
    name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    candidates = [
        TARGET_DIR / profile / name for profile in ("release", "debug")
    ]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


BINARY = _daemon_binary()

pytestmark = [
    pytest.mark.skipif(BINARY is None, reason="remy-daemon binary not built"),
    pytest.mark.skipif(
        not TREE_SITTER_AVAILABLE, reason="tree-sitter backend unavailable"
    ),
]


def _rust_scan(root: Path, db: Path, *extra: str) -> dict:
    output = subprocess.check_output(
        [str(BINARY), "scan", "--root", str(root), "--db", str(db), "--result-json", *extra],
        text=True,
    )
    return json.loads(output.strip().splitlines()[-1])


def _python_scan(root: Path) -> Path:
    from struct_scan import StructScanner

    scanner = StructScanner(str(root))
    try:
        result = scanner.scan_all()
        assert result.status.value == "success", result.errors
    finally:
        scanner.db.close()
    return root / ".claude" / "logic_index.db"


def _phase1_state(db_path: Path):
    db = sqlite3.connect(str(db_path))
    try:
        return oracle_normalization.phase1_state(db)
    finally:
        db.close()


@pytest.fixture
def c_project(tmp_path: Path) -> Path:
    destination = tmp_path / "corpus"
    shutil.copytree(C_CORPUS, destination)
    return destination


def _phase1_findings(left_db: Path, right_db: Path):
    return oracle_comparator.compare_dbs(
        left_db,
        right_db,
        views=oracle_classification.PHASE1_VIEWS,
        row_filters=oracle_normalization.PHASE1_ROW_FILTERS,
    )


def test_fixture_diff_has_zero_blocking_findings(c_project: Path):
    python_db = _python_scan(c_project)
    rust_db = c_project.parent / "rust.db"
    report = _rust_scan(c_project, rust_db)
    assert report["outcome"] == "success"

    findings = _phase1_findings(python_db, rust_db)
    blocking = oracle_comparator.blocking(findings)
    assert blocking == [], [
        (f.view, f.category, f.key, f.column) for f in blocking
    ]
    informational = {(f.view, f.column) for f in findings}
    assert informational <= {("files", "parser_backend"), ("files", "parser_environment")}


def test_rust_jobs_commute_on_phase1_projection(c_project: Path):
    _python_scan(c_project)  # materializes the shared logic_index_config
    states = []
    for jobs in ("1", "2", "8"):
        db = c_project.parent / f"rust_jobs_{jobs}.db"
        report = _rust_scan(c_project, db, "--jobs", jobs)
        assert report["outcome"] == "success"
        states.append(_phase1_state(db))
    assert states[0] == states[1] == states[2]


def test_rust_incremental_equals_full_scan(c_project: Path):
    _python_scan(c_project)
    full_db = c_project.parent / "rust_full.db"
    incremental_db = c_project.parent / "rust_incremental.db"
    full_report = _rust_scan(c_project, full_db)
    for rel in full_report["successful_paths"]:
        report = _rust_scan(c_project, incremental_db, "--files", rel)
        assert report["outcome"] == "success"
    assert _phase1_state(full_db) == _phase1_state(incremental_db)


def test_rust_scan_result_matches_schema_v1(c_project: Path):
    _python_scan(c_project)
    report = _rust_scan(c_project, c_project.parent / "rust_schema.db")
    assert report["type"] == "scan_result"
    assert report["schema_version"] == 1
    assert report["outcome"] == "success"
    assert report["postprocess_complete"] is True
    assert report["errors"] == []
    assert sorted(report["successful_paths"]) == report["successful_paths"]


def test_rust_db_is_readable_with_python_schema_expectations(c_project: Path):
    _python_scan(c_project)
    rust_db = c_project.parent / "rust_meta.db"
    _rust_scan(c_project, rust_db)
    db = sqlite3.connect(str(rust_db))
    try:
        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        assert version == ("12.0.0",)
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"files", "symbols", "symbol_occurrences", "edges", "patterns"} <= tables
    finally:
        db.close()
