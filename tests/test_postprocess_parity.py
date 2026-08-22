"""Summary-invalidation and postprocess-parity assertions (R3.4).

The blocking diff deliberately excludes the DIAGNOSTIC tables, so the
summary state machine (stale transitions, initial summaries, change
counters) is verified here by value-level comparison between the Python
oracle and the Rust scanner after identical perturbation sequences.
Timestamp columns (created_at / updated_at) are excluded from every
comparison.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = REPO_ROOT / "remy-daemon" / "target"

sys.path.insert(0, str(REPO_ROOT / "remy-src"))
import remy_config

try:
    import parsers.python_parser as python_parser

    PYTHON_PARSER_AVAILABLE = python_parser is not None
except Exception:  # pragma: no cover - import environment issues equal skip
    PYTHON_PARSER_AVAILABLE = False


def _daemon_binary() -> Path | None:
    name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    candidates = [TARGET_DIR / profile / name for profile in ("release", "debug")]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


BINARY = _daemon_binary()

pytestmark = pytest.mark.skipif(BINARY is None, reason="remy-daemon binary not built")


def _python_scan_all(root: Path) -> None:
    from struct_scan import StructScanner

    scanner = StructScanner(str(root))
    try:
        result = scanner.scan_all()
        assert result.status.value == "success", result.errors
    finally:
        scanner.db.close()


def _python_scan_files(root: Path, files: list[str]):
    from struct_scan import StructScanner

    scanner = StructScanner(str(root))
    try:
        return scanner.scan_files(files)
    finally:
        scanner.db.close()


def _rust_scan(root: Path, db: Path, *extra: str) -> dict:
    completed = subprocess.run(
        [str(BINARY), "scan", "--root", str(root), "--db", str(db), "--result-json", *extra],
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _summary_state(db_path: Path):
    db = sqlite3.connect(str(db_path))
    try:
        versions = db.execute(
            "SELECT node_kind, node_ref, version, summary, status "
            "FROM summary_versions ORDER BY node_kind, node_ref, version"
        ).fetchall()
        counters = db.execute(
            "SELECT node_kind, node_ref, child_change_count "
            "FROM node_change_counters ORDER BY node_kind, node_ref"
        ).fetchall()
        return versions, counters
    finally:
        db.close()


def _retrieval_state(db_path: Path):
    db = sqlite3.connect(str(db_path))
    try:
        return db.execute(
            "SELECT node_kind, node_ref, language, symbol_type, file_path, name, "
            "name_tokens, signature, summary_short, summary_full, content_hash, "
            "source_version FROM retrieval_documents ORDER BY node_kind, node_ref"
        ).fetchall()
    finally:
        db.close()


@pytest.fixture
def twin_projects(tmp_path: Path):
    """One source tree scanned into two databases: the Python oracle's
    (project .claude) and the Rust scanner's (isolated path)."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(
        'def documented():\n    """Documented helper."""\n    return 1\n\n\n'
        "def plain():\n    return documented()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "b.py").write_text(
        "from pkg.a import documented\n\n\ndef caller():\n    return documented()\n",
        encoding="utf-8",
    )
    rust_db = tmp_path / "rust.db"
    _python_scan_all(root)
    assert _rust_scan(root, rust_db)["outcome"] == "success"
    return root, root / ".claude" / "logic_index.db", rust_db


@pytest.mark.skipif(not PYTHON_PARSER_AVAILABLE, reason="python parser unavailable")
class TestSummaryInvalidationParity:
    def test_initial_summaries_agree_after_full_scan(self, twin_projects):
        _root, python_db, rust_db = twin_projects
        assert _summary_state(python_db) == _summary_state(rust_db)
        versions, _ = _summary_state(python_db)
        doc_rows = [row for row in versions if row[1].endswith("::documented")]
        assert doc_rows == [
            (
                "symbol",
                "pkg/a.py::documented",
                1,
                '{"short": "[Doc] Documented helper.", "full": null}',
                "ok",
            )
        ]

    def test_hash_change_marks_stale_and_appends_new_version(self, twin_projects):
        root, python_db, rust_db = twin_projects
        (root / "pkg" / "a.py").write_text(
            'def documented():\n    """Documented helper."""\n    return 2\n\n\n'
            "def plain():\n    return documented()\n",
            encoding="utf-8",
        )
        result = _python_scan_files(root, ["pkg/a.py"])
        assert result.status.value == "success"
        assert _rust_scan(root, rust_db, "--files", "pkg/a.py")["outcome"] == "success"
        assert _summary_state(python_db) == _summary_state(rust_db)
        versions, _ = _summary_state(python_db)
        doc_rows = [
            (row[2], row[4]) for row in versions if row[1].endswith("::documented")
        ]
        assert doc_rows == [(1, "stale"), (2, "ok")]

    def test_symbol_set_change_marks_file_summary_stale(self, twin_projects):
        root, python_db, rust_db = twin_projects
        for db_path in (python_db, rust_db):
            db = sqlite3.connect(str(db_path))
            db.execute(
                "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
                "VALUES ('file', 'pkg/a.py', 1, '{\"short\": \"File summary.\", \"full\": null}', 'ok', 't')"
            )
            db.commit()
            db.close()
        (root / "pkg" / "a.py").write_text(
            'def documented():\n    """Documented helper."""\n    return 1\n',
            encoding="utf-8",
        )
        assert _python_scan_files(root, ["pkg/a.py"]).status.value == "success"
        assert _rust_scan(root, rust_db, "--files", "pkg/a.py")["outcome"] == "success"
        assert _summary_state(python_db) == _summary_state(rust_db)
        versions, _ = _summary_state(python_db)
        file_rows = [(row[2], row[4]) for row in versions if row[0] == "file"]
        assert file_rows == [(1, "stale")]

    def test_cluster_member_change_marks_cluster_summary_stale(self, twin_projects):
        root, python_db, rust_db = twin_projects
        for db_path in (python_db, rust_db):
            db = sqlite3.connect(str(db_path))
            db.execute(
                "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
                "VALUES ('cluster', 'pkg', 1, '{\"short\": \"Pkg cluster.\", \"full\": null}', 'ok', 't')"
            )
            db.commit()
            db.close()
        (root / "pkg" / "c.py").write_text(
            "from pkg.a import documented\n\n\ndef extra():\n    return documented()\n",
            encoding="utf-8",
        )
        assert _python_scan_files(root, ["pkg/c.py"]).status.value == "success"
        assert _rust_scan(root, rust_db, "--files", "pkg/c.py")["outcome"] == "success"
        assert _summary_state(python_db) == _summary_state(rust_db)
        versions, counters = _summary_state(python_db)
        cluster_rows = [(row[2], row[4]) for row in versions if row[0] == "cluster"]
        assert cluster_rows == [(1, "stale")]
        assert ("cluster", "pkg", 0) in counters

    def test_retrieval_documents_agree_including_content_hash(self, twin_projects):
        _root, python_db, rust_db = twin_projects
        python_rows = _retrieval_state(python_db)
        assert python_rows == _retrieval_state(rust_db)
        assert all(len(row[10]) == 64 for row in python_rows)

    def test_incremental_path_keeps_retrieval_and_summary_parity(self, twin_projects):
        root, python_db, rust_db = twin_projects
        (root / "pkg" / "a.py").write_text(
            'def documented():\n    """Documented helper."""\n    return 7\n\n\n'
            "def freshly_added():\n    return documented()\n",
            encoding="utf-8",
        )
        assert _python_scan_files(root, ["pkg/a.py"]).status.value == "success"
        assert _rust_scan(root, rust_db, "--files", "pkg/a.py")["outcome"] == "success"
        assert _summary_state(python_db) == _summary_state(rust_db)
        assert _retrieval_state(python_db) == _retrieval_state(rust_db)


@pytest.mark.skipif(not PYTHON_PARSER_AVAILABLE, reason="python parser unavailable")
class TestPostprocessFailureInjection:
    def test_python_incremental_failure_keeps_old_projection(self, twin_projects, monkeypatch):
        root, python_db, _rust_db = twin_projects
        before = _retrieval_state(python_db)
        (root / "pkg" / "a.py").write_text("def documented():\n    return 3\n", encoding="utf-8")
        from struct_scan import StructScanner

        scanner = StructScanner(str(root))
        try:
            monkeypatch.setenv("REMY_FILE_KIND_MIN_SYMBOLS", "not-an-int")
            result = scanner.scan_files(["pkg/a.py"])
        finally:
            scanner.db.close()
        assert result.status.value == "failed"
        assert not result.postprocess_complete
        assert _retrieval_state(python_db) == before

    def test_rust_invalid_config_fails_before_touching_db(self, twin_projects, monkeypatch):
        root, _python_db, rust_db = twin_projects
        before = rust_db.read_bytes()
        monkeypatch.setenv("REMY_FILE_KIND_MIN_SYMBOLS", "not-an-int")
        completed = subprocess.run(
            [str(BINARY), "scan", "--root", str(root), "--db", str(rust_db), "--result-json"],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        assert completed.returncode == 1
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        assert report["outcome"] == "failed"
        assert "REMY_FILE_KIND_MIN_SYMBOLS" in report["errors"][0]["message"]
        assert rust_db.read_bytes() == before


@pytest.mark.skipif(not PYTHON_PARSER_AVAILABLE, reason="python parser unavailable")
class TestMcpReadsRustDatabase:
    def test_mcp_queries_agree_between_databases(self, twin_projects):
        _root, python_db, rust_db = twin_projects
        sys.path.insert(0, str(REPO_ROOT / "remy-src"))
        import index_mcp_common
        import index_mcp_facts
        import index_mcp_graph

        results = []
        for db_path in (python_db, rust_db):
            with index_mcp_common.database_override(db_path):
                results.append(
                    (
                        index_mcp_facts.query_symbol_impl("documented"),
                        index_mcp_graph.query_callers_impl(
                            "documented", depth=2, include_ambiguous=False, static_only=False
                        ),
                        index_mcp_facts.query_cluster_summary_impl(),
                        index_mcp_facts.query_file_summary_impl("pkg/a.py"),
                    )
                )
        assert results[0] == results[1]
        assert "documented" in str(results[1][0])

    def test_full_scan_records_source_commit_in_git_repo(self, tmp_path):
        root = tmp_path / "repo"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        (root / "lib.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
            cwd=root,
            check=True,
        )
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        rust_db = tmp_path / "rust_git.db"
        assert _rust_scan(root, rust_db)["outcome"] == "success"
        db = sqlite3.connect(str(rust_db))
        try:
            stored = db.execute(
                "SELECT value FROM meta WHERE key='source_commit'"
            ).fetchone()
        finally:
            db.close()
        assert stored == (head,)


class TestNarrowConfigContract:
    """Locks the Python registry values replicated by scanner-core's
    rconfig.rs; a drift on either side must update both and this snapshot."""

    EXPECTED = {
        "REMY_LOGIC_INDEX_FILTER_SMALL": ("bool", "false", None, None),
        "REMY_CLUSTER_DENSITY_THRESHOLD": ("float", "0.5", 0.0, None),
        "REMY_CLUSTER_MAX_SIZE": ("int", "15", 2, 200),
        "REMY_CLUSTER_ENTRY_COUNT": ("int", "3", 1, 20),
        "REMY_SYNTH_INTERFACE_FANOUT_CAP": ("int", "10", 1, 100),
        "REMY_SYNTH_EVENT_FANOUT_CAP": ("int", "20", 1, 200),
        "REMY_RESOLVE_FANOUT_CAP": ("int", "10", 1, 100),
        "REMY_RESOLVE_SCORE_SAME_FILE": ("int", "2", 0, 100),
        "REMY_RESOLVE_SCORE_DIRECT_IMPORT": ("int", "1", 0, 100),
        "REMY_RESOLVE_SCORE_GLOBAL": ("int", "0", 0, 100),
        "REMY_FILE_KIND_MIN_SYMBOLS": ("int", "5", 1, 50),
        "REMY_FILE_KIND_LOW_COHESION_THRESHOLD": ("float", "0.25", 0.0, 1.0),
        "REMY_INDEX_SCAN_LOCK_TIMEOUT": ("float", "30", 0, 300),
        "REMY_STRUCT_SCAN_TIMEOUT": ("int", "60", 10, 300),
        "REMY_FULL_SCAN_TIMEOUT": ("int", "1800", 60, 86400),
    }

    def test_registry_matches_rust_replication_snapshot(self):
        for key, (value_type, default, minimum, maximum) in self.EXPECTED.items():
            spec = remy_config.FIELD_SPECS[key]
            assert spec.value_type == value_type, key
            assert spec.default == default, key
            assert spec.minimum == minimum, key
            assert spec.maximum == maximum, key
