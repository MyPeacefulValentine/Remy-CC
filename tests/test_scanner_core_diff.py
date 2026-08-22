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
CORPUS_ROOT = REPO_ROOT / "oracle" / "fixtures" / "corpus"
C_CORPUS = CORPUS_ROOT / "c"
LANGUAGE_CORPORA = ("c", "python", "ts", "rust")
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


def _full_findings(left_db: Path, right_db: Path):
    return oracle_comparator.compare_dbs(left_db, right_db)


def _full_state(db_path: Path):
    db = sqlite3.connect(str(db_path))
    try:
        return oracle_normalization.oracle_state(db)
    finally:
        db.close()


# Full-view informational findings may also cover the ALLOWED_DIFF summary
# columns; fact columns stay EXACT and would surface as blocking instead.
FULL_VIEW_ALLOWED_INFO = {
    ("files", "parser_backend"),
    ("files", "parser_environment"),
    ("retrieval_documents", "summary_short"),
    ("retrieval_documents", "summary_full"),
    ("retrieval_documents", "content_hash"),
}


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


@pytest.mark.parametrize("corpus", LANGUAGE_CORPORA)
def test_language_corpus_diff_has_zero_blocking_findings(tmp_path: Path, corpus: str):
    destination = tmp_path / corpus
    shutil.copytree(CORPUS_ROOT / corpus, destination)
    python_db = _python_scan(destination)
    rust_db = tmp_path / f"rust_{corpus}.db"
    report = _rust_scan(destination, rust_db)
    assert report["outcome"] == "success"

    findings = _phase1_findings(python_db, rust_db)
    blocking = oracle_comparator.blocking(findings)
    assert blocking == [], [
        (f.view, f.category, f.key, f.column) for f in blocking
    ]
    informational = {(f.view, f.column) for f in findings}
    assert informational <= {("files", "parser_backend"), ("files", "parser_environment")}


def test_mixed_language_project_diff_and_jobs_commute(tmp_path: Path):
    destination = tmp_path / "mixed"
    destination.mkdir()
    for corpus in LANGUAGE_CORPORA:
        shutil.copytree(CORPUS_ROOT / corpus, destination / corpus)
    python_db = _python_scan(destination)
    rust_db = tmp_path / "rust_mixed.db"
    report = _rust_scan(destination, rust_db)
    assert report["outcome"] == "success"
    assert oracle_comparator.blocking(_phase1_findings(python_db, rust_db)) == []

    states = []
    for jobs in ("1", "8"):
        db = tmp_path / f"rust_mixed_jobs_{jobs}.db"
        assert _rust_scan(destination, db, "--jobs", jobs)["outcome"] == "success"
        states.append(_phase1_state(db))
    assert states[0] == states[1]


@pytest.mark.parametrize("corpus", LANGUAGE_CORPORA)
def test_full_view_language_corpus_diff_has_zero_blocking(tmp_path: Path, corpus: str):
    destination = tmp_path / corpus
    shutil.copytree(CORPUS_ROOT / corpus, destination)
    python_db = _python_scan(destination)
    rust_db = tmp_path / f"rust_full_{corpus}.db"
    report = _rust_scan(destination, rust_db)
    assert report["outcome"] == "success"

    findings = _full_findings(python_db, rust_db)
    blocking = oracle_comparator.blocking(findings)
    assert blocking == [], [
        (f.view, f.category, f.key, f.column, f.left, f.right) for f in blocking
    ]
    informational = {(f.view, f.column) for f in findings}
    assert informational <= FULL_VIEW_ALLOWED_INFO


def test_full_view_mixed_project_diff_and_jobs_commute(tmp_path: Path):
    destination = tmp_path / "mixed"
    destination.mkdir()
    for corpus in LANGUAGE_CORPORA:
        shutil.copytree(CORPUS_ROOT / corpus, destination / corpus)
    python_db = _python_scan(destination)
    rust_db = tmp_path / "rust_full_mixed.db"
    report = _rust_scan(destination, rust_db)
    assert report["outcome"] == "success"
    blocking = oracle_comparator.blocking(_full_findings(python_db, rust_db))
    assert blocking == [], [
        (f.view, f.category, f.key, f.column) for f in blocking
    ]

    states = []
    for jobs in ("1", "8"):
        db = tmp_path / f"rust_full_jobs_{jobs}.db"
        assert _rust_scan(destination, db, "--jobs", jobs)["outcome"] == "success"
        states.append(_full_state(db))
    assert states[0] == states[1]


def test_full_view_incremental_equals_full_scan(tmp_path: Path):
    destination = tmp_path / "rust"
    shutil.copytree(CORPUS_ROOT / "rust", destination)
    _python_scan(destination)
    full_db = tmp_path / "rust_full.db"
    incremental_db = tmp_path / "rust_incremental.db"
    full_report = _rust_scan(destination, full_db)
    assert full_report["outcome"] == "success"
    for rel in full_report["successful_paths"]:
        report = _rust_scan(destination, incremental_db, "--files", rel)
        assert report["outcome"] == "success"
    assert _full_state(full_db) == _full_state(incremental_db)


def test_cross_file_trait_impl_agrees_across_implementations(tmp_path: Path):
    destination = tmp_path / "rust"
    shutil.copytree(CORPUS_ROOT / "rust", destination)
    python_db = _python_scan(destination)
    rust_db = tmp_path / "rust_traits.db"
    assert _rust_scan(destination, rust_db)["outcome"] == "success"

    for db_path in (python_db, rust_db):
        db = sqlite3.connect(str(db_path))
        try:
            kind_bases = db.execute(
                "SELECT bases FROM symbols WHERE name='Kind'"
            ).fetchone()
            assert kind_bases == ('["HasArea"]',), db_path
            gauges = db.execute(
                "SELECT file_path, bases FROM symbols WHERE short_name='Gauge' "
                "ORDER BY file_path"
            ).fetchall()
            assert gauges == [("impls.rs", None), ("widgets.rs", None)], db_path
            cross_edge = db.execute(
                "SELECT COUNT(*) FROM edges WHERE via='trait-impl' "
                "AND callee_qualified='impls.rs::Kind.area'"
            ).fetchone()
            assert cross_edge == (1,), db_path
        finally:
            db.close()


def test_rust_scan_refuses_non_current_schema_version(tmp_path: Path):
    destination = tmp_path / "rust"
    shutil.copytree(CORPUS_ROOT / "rust", destination)
    _python_scan(destination)
    stale_db = tmp_path / "stale.db"
    db = sqlite3.connect(str(stale_db))
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('version', '11.0.0')")
    db.commit()
    before = stale_db.read_bytes()
    db.close()
    completed = subprocess.run(
        [str(BINARY), "scan", "--root", str(destination), "--db", str(stale_db), "--result-json"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["outcome"] == "failed"
    assert "11.0.0" in report["errors"][0]["message"]
    assert stale_db.read_bytes() == before


def test_python_failure_mapping_matches_oracle(tmp_path: Path):
    destination = tmp_path / "pyfail"
    shutil.copytree(CORPUS_ROOT / "python", destination)
    python_db = _python_scan(destination)
    rust_db = tmp_path / "rust_pyfail.db"
    report = _rust_scan(destination, rust_db)
    assert report["outcome"] == "success"
    assert oracle_comparator.blocking(_phase1_findings(python_db, rust_db)) == []

    db = sqlite3.connect(str(rust_db))
    try:
        for path in ("broken_syntax.py", "bom_prefixed.py"):
            row = db.execute(
                "SELECT COUNT(*) FROM files WHERE path=?", (path,)
            ).fetchone()
            assert row == (1,), path
            symbols = db.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_path=?", (path,)
            ).fetchone()
            assert symbols == (0,), path
    finally:
        db.close()


def _python_scan_files(root: Path, files: list[str]):
    from struct_scan import StructScanner

    scanner = StructScanner(str(root))
    try:
        return scanner.scan_files(files)
    finally:
        scanner.db.close()


def test_rust_incremental_exclusion_sweep_matches_python(tmp_path: Path):
    destination = tmp_path / "corpus"
    shutil.copytree(C_CORPUS, destination)
    (destination / "legacy").mkdir()
    (destination / "legacy" / "old.c").write_text(
        "int legacy_fn(void) { return 1; }\n", encoding="utf-8"
    )
    python_db = _python_scan(destination)
    rust_db = tmp_path / "rust.db"
    assert _rust_scan(destination, rust_db)["outcome"] == "success"

    config_path = destination / ".claude" / "logic_index_config"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "!legacy/\n", encoding="utf-8"
    )
    python_result = _python_scan_files(destination, ["legacy/old.c"])
    assert python_result.status.value == "success", python_result.errors
    rust_report = _rust_scan(destination, rust_db, "--files", "legacy/old.c")
    assert rust_report["outcome"] == "success"
    assert rust_report["successful_paths"] == ["legacy/old.c"]
    assert "legacy/old.c" in rust_report["deleted_paths"]

    for db_path in (python_db, rust_db):
        db = sqlite3.connect(str(db_path))
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM files WHERE path LIKE 'legacy/%'"
            ).fetchone()
            assert count == (0,), db_path
        finally:
            db.close()
    assert oracle_comparator.blocking(_full_findings(python_db, rust_db)) == []


def test_rust_full_scan_sweep_matches_python(tmp_path: Path):
    destination = tmp_path / "corpus"
    shutil.copytree(C_CORPUS, destination)
    (destination / "legacy").mkdir()
    (destination / "legacy" / "old.c").write_text(
        "int legacy_fn(void) { return 1; }\n", encoding="utf-8"
    )
    _python_scan(destination)
    rust_db = tmp_path / "rust.db"
    assert _rust_scan(destination, rust_db)["outcome"] == "success"

    config_path = destination / ".claude" / "logic_index_config"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "!legacy/\n", encoding="utf-8"
    )
    python_db = _python_scan(destination)
    rust_report = _rust_scan(destination, rust_db)
    assert rust_report["outcome"] == "success"
    assert "legacy/old.c" in rust_report["deleted_paths"]

    for db_path in (python_db, rust_db):
        db = sqlite3.connect(str(db_path))
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM files WHERE path LIKE 'legacy/%'"
            ).fetchone()
            assert count == (0,), db_path
        finally:
            db.close()
    assert oracle_comparator.blocking(_full_findings(python_db, rust_db)) == []


def test_rust_incremental_identity_invalid_rescan_matches_python(tmp_path: Path):
    destination = tmp_path / "corpus"
    shutil.copytree(C_CORPUS, destination)
    python_db = _python_scan(destination)
    rust_db = tmp_path / "rust.db"
    full_report = _rust_scan(destination, rust_db)
    assert full_report["outcome"] == "success"
    tampered, requested = full_report["successful_paths"][:2]

    for db_path in (python_db, rust_db):
        db = sqlite3.connect(str(db_path))
        db.execute(
            "UPDATE files SET parser_contract_version='0' WHERE path=?", (tampered,)
        )
        db.commit()
        db.close()

    python_result = _python_scan_files(destination, [requested])
    assert python_result.status.value == "success", python_result.errors
    assert tampered in python_result.discovered_paths
    rust_report = _rust_scan(destination, rust_db, "--files", requested)
    assert rust_report["outcome"] == "success"
    assert tampered in rust_report["successful_paths"]

    db = sqlite3.connect(str(rust_db))
    try:
        contract = db.execute(
            "SELECT parser_contract_version FROM files WHERE path=?", (tampered,)
        ).fetchone()
        assert contract != ("0",)
    finally:
        db.close()
    assert oracle_comparator.blocking(_full_findings(python_db, rust_db)) == []


def test_rust_progress_json_lines_contract(c_project: Path):
    _python_scan(c_project)
    output = subprocess.check_output(
        [
            str(BINARY), "scan",
            "--root", str(c_project),
            "--db", str(c_project.parent / "rust_progress.db"),
            "--result-json",
            "--progress-json",
        ],
        text=True,
    )
    lines = [json.loads(line) for line in output.strip().splitlines()]
    assert all(line["type"] in {"progress", "scan_result"} for line in lines)
    progress = [line for line in lines if line["type"] == "progress"]
    assert progress[0]["stage"] == "lock_acquired"
    assert {"parse_done", "postprocess_done"} <= {line["stage"] for line in progress}
    final = lines[-1]
    assert final["type"] == "scan_result"
    assert final["schema_version"] == 1
    assert final["outcome"] == "success"


def _assert_no_candidate_orphans(db_path: Path):
    db = sqlite3.connect(str(db_path))
    try:
        orphans = db.execute(
            "SELECT COUNT(*) FROM edge_candidates ec "
            "LEFT JOIN edges e ON e.id = ec.edge_id WHERE e.id IS NULL"
        ).fetchone()
        assert orphans == (0,)
    finally:
        db.close()


@pytest.fixture
def perturbation_project(tmp_path: Path) -> Path:
    """Python tree with global-tier short-name ties (edge_candidates rows),
    shared by the incremental-vs-full cases."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / ".claude").mkdir()
    (root / ".claude" / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(
        "def helper():\n    return 1\n\n\ndef twin():\n    return 2\n",
        encoding="utf-8",
    )
    (root / "pkg" / "extra.py").write_text(
        "def twin():\n    return 3\n", encoding="utf-8"
    )
    (root / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef main():\n    helper()\n    twin()\n",
        encoding="utf-8",
    )
    return root


def test_rust_incremental_perturbations_match_python_full(
    tmp_path: Path, perturbation_project: Path
):
    root = perturbation_project
    rust_db = tmp_path / "rust_inc.db"
    _python_scan(root)
    assert _rust_scan(root, rust_db)["outcome"] == "success"

    perturbations = [
        # Cross-file rename: the twin tie in pkg/extra.py re-resolves.
        (
            "pkg/util.py",
            "def helper_renamed():\n    return 1\n\n\ndef twin():\n    return 2\n",
        ),
        # New file adds a lexicographically earlier twin candidate.
        ("pkg/aaa.py", "def twin():\n    return 4\n"),
        # Remove the import-tier caller's edges entirely.
        ("app.py", "def main():\n    return 0\n"),
    ]
    for rel, content in perturbations:
        (root / rel).write_text(content, encoding="utf-8")
        result = _python_scan_files(root, [rel])
        assert result.status.value == "success", result.errors
        assert _rust_scan(root, rust_db, "--files", rel)["outcome"] == "success"
        python_db = root / ".claude" / "logic_index.db"
        blocking = oracle_comparator.blocking(_full_findings(python_db, rust_db))
        assert blocking == [], (rel, [(f.view, f.key, f.column) for f in blocking])
        _assert_no_candidate_orphans(rust_db)

    (root / "pkg" / "extra.py").unlink()
    result = _python_scan_files(root, ["pkg/extra.py"])
    assert result.status.value == "success", result.errors
    assert _rust_scan(root, rust_db, "--files", "pkg/extra.py")["outcome"] == "success"
    python_db = root / ".claude" / "logic_index.db"
    assert oracle_comparator.blocking(_full_findings(python_db, rust_db)) == []
    _assert_no_candidate_orphans(rust_db)


def test_rust_incremental_import_binding_host_matches_full(tmp_path: Path):
    """Adding/removing a .py file flips another file's import-binding
    resolution (external ↔ unique suffix hit); the host file is never in
    the delta, so only the binding-host edge reset can reach its edges."""
    root = tmp_path / "proj"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
    (root / "host.py").write_text(
        "from vendor.mod import ext_fn\n\n\ndef use():\n    return ext_fn()\n",
        encoding="utf-8",
    )
    (root / "other.py").write_text(
        "def ext_fn():\n    return 0\n", encoding="utf-8"
    )
    python_db = _python_scan(root)
    rust_db = tmp_path / "rust_hosts.db"
    assert _rust_scan(root, rust_db)["outcome"] == "success"
    db = sqlite3.connect(str(rust_db))
    try:
        unresolved = db.execute(
            "SELECT callee_qualified FROM edges WHERE callee='ext_fn'"
        ).fetchone()
        assert unresolved == (None,), "external suppression must hold initially"
    finally:
        db.close()

    (root / "vendor").mkdir()
    (root / "vendor" / "mod.py").write_text(
        "def ext_fn():\n    return 1\n", encoding="utf-8"
    )
    for rel in ["vendor/mod.py"]:
        assert _python_scan_files(root, [rel]).status.value == "success"
        assert _rust_scan(root, rust_db, "--files", rel)["outcome"] == "success"
    assert oracle_comparator.blocking(_full_findings(python_db, rust_db)) == []
    db = sqlite3.connect(str(rust_db))
    try:
        resolved = db.execute(
            "SELECT callee_qualified FROM edges WHERE callee='ext_fn'"
        ).fetchone()
        assert resolved == ("vendor/mod.py::ext_fn",)
    finally:
        db.close()

    (root / "vendor" / "mod.py").unlink()
    assert _python_scan_files(root, ["vendor/mod.py"]).status.value == "success"
    assert _rust_scan(root, rust_db, "--files", "vendor/mod.py")["outcome"] == "success"
    assert oracle_comparator.blocking(_full_findings(python_db, rust_db)) == []
    db = sqlite3.connect(str(rust_db))
    try:
        unresolved = db.execute(
            "SELECT callee_qualified FROM edges WHERE callee='ext_fn'"
        ).fetchone()
        assert unresolved == (None,), "removal must restore external suppression"
    finally:
        db.close()


def test_rust_incremental_fanout_cap_overflow_matches_full(
    tmp_path: Path, perturbation_project: Path, monkeypatch
):
    """A delta file pushing an observer signal past the fanout cap drops
    an inferred edge between two non-delta pkg files, and that drop flips
    the pkg cluster below the density threshold — neither endpoint is in
    the delta, so only the synth snapshot diff can reach the cluster."""
    root = perturbation_project
    (root / "pkg" / "emitter.py").write_text(
        "from pkg.util import helper\n\n\nclass Hub:\n"
        "    def fire(self):\n        helper()\n"
        "        for cb in self.hooks:\n            cb()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "reg_b.py").write_text(
        "from pkg.util import helper\n\n\ndef on_b():\n    return helper()\n\n\n"
        "class RegB:\n    def setup(self):\n        self.hooks.append(on_b)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REMY_SYNTH_EVENT_FANOUT_CAP", "1")
    _python_scan(root)
    rust_db = tmp_path / "rust_fanout.db"
    assert _rust_scan(root, rust_db)["outcome"] == "success"
    db = sqlite3.connect(str(rust_db))
    try:
        observer = db.execute(
            "SELECT COUNT(*) FROM edges WHERE via='observer'"
        ).fetchone()
        assert observer == (1,), "fixture must start with one observer edge"
        clusters = db.execute("SELECT name FROM clusters").fetchall()
        assert clusters == [("pkg",)], "fixture must start with the pkg cluster"
    finally:
        db.close()

    (root / "reg_c.py").write_text(
        "def on_c():\n    return 2\n\n\nclass RegC:\n"
        "    def setup(self):\n        self.hooks.append(on_c)\n",
        encoding="utf-8",
    )
    result = _python_scan_files(root, ["reg_c.py"])
    assert result.status.value == "success", result.errors
    assert _rust_scan(root, rust_db, "--files", "reg_c.py")["outcome"] == "success"
    python_db = root / ".claude" / "logic_index.db"
    blocking = oracle_comparator.blocking(_full_findings(python_db, rust_db))
    assert blocking == [], [(f.view, f.key, f.column) for f in blocking]
    db = sqlite3.connect(str(rust_db))
    try:
        observer = db.execute(
            "SELECT COUNT(*) FROM edges WHERE via='observer'"
        ).fetchone()
        assert observer == (0,), "cap overflow must drop the emitter's edges"
        clusters = db.execute("SELECT name FROM clusters").fetchall()
        assert clusters == [], "density must fall below threshold"
    finally:
        db.close()


DOCSTRING_HASH_CORPUS = {
    "plain.py": 'def f(a):\n    """Doc with # inside."""\n    return a\n',
    "variants.py": (
        "def raw(a):\n    r'''Raw doc.'''\n    return a\n\n\n"
        'class C:\n    """Class doc."""\n\n    def m(self):\n        return 1\n'
    ),
    "concat.py": 'def g(a):\n    "part one " "part two"\n    return a\n',
    "not_doc.py": 'def h(a):\n    s = """assigned, not a docstring"""\n    return s\n',
    "plain_no_doc.py": 'def k(a):\n    return a\n',
}


def _write_docstring_corpus(root: Path) -> None:
    root.mkdir()
    claude = root / ".claude"
    claude.mkdir()
    (claude / "logic_index_config").write_text("!.git/\n!.claude/\n", encoding="utf-8")
    for name, source in DOCSTRING_HASH_CORPUS.items():
        (root / name).write_text(source, encoding="utf-8")


def _symbol_hashes(db_path: Path) -> dict:
    db = sqlite3.connect(str(db_path))
    try:
        return dict(db.execute(
            "SELECT file_path || ':' || name, hash FROM symbols").fetchall())
    finally:
        db.close()


def test_docstring_hash_exclusion_matches_across_implementations(tmp_path: Path):
    destination = tmp_path / "docstring_corpus"
    _write_docstring_corpus(destination)
    python_db = _python_scan(destination)
    python_hashes = _symbol_hashes(python_db)
    rust_db = tmp_path / "rust_docstring.db"
    assert _rust_scan(destination, rust_db)["outcome"] == "success"
    assert _symbol_hashes(rust_db) == python_hashes
    assert oracle_comparator.blocking(_phase1_findings(python_db, rust_db)) == []


def test_docstring_only_edit_is_hash_neutral_in_rust(tmp_path: Path):
    destination = tmp_path / "doc_edit"
    _write_docstring_corpus(destination)
    base_db = tmp_path / "rust_base.db"
    assert _rust_scan(destination, base_db)["outcome"] == "success"
    base = _symbol_hashes(base_db)

    (destination / "plain.py").write_text(
        'def f(a):\n    """Entirely new documentation."""\n    return a\n',
        encoding="utf-8",
    )
    (destination / "not_doc.py").write_text(
        'def h(a):\n    s = """changed assigned literal"""\n    return s\n',
        encoding="utf-8",
    )
    edited_db = tmp_path / "rust_edited.db"
    assert _rust_scan(destination, edited_db)["outcome"] == "success"
    edited = _symbol_hashes(edited_db)

    assert edited["plain.py:f"] == base["plain.py:f"], (
        "docstring-only edit must keep the symbol hash")
    assert edited["not_doc.py:h"] != base["not_doc.py:h"], (
        "a non-docstring triple-quoted literal stays inside the hash")


def test_relative_root_scan_matches_absolute_root_scan(tmp_path: Path):
    destination = tmp_path / "relroot"
    _write_docstring_corpus(destination)
    abs_db = tmp_path / "abs.db"
    assert _rust_scan(destination, abs_db)["outcome"] == "success"

    rel_db = tmp_path / "rel.db"
    output = subprocess.check_output(
        [str(BINARY), "scan", "--root", "relroot", "--db", str(rel_db), "--result-json"],
        text=True,
        cwd=str(tmp_path),
    )
    assert json.loads(output.strip().splitlines()[-1])["outcome"] == "success"

    inc_output = subprocess.check_output(
        [str(BINARY), "scan", "--root", "relroot", "--db", str(rel_db),
         "--result-json", "--files", "plain.py"],
        text=True,
        cwd=str(tmp_path),
    )
    assert json.loads(inc_output.strip().splitlines()[-1])["outcome"] == "success"

    db = sqlite3.connect(str(rel_db))
    try:
        paths = [row[0] for row in db.execute("SELECT path FROM files")]
    finally:
        db.close()
    assert paths, "relative-root scan must index the corpus"
    assert all(not path.startswith("..") for path in paths), paths
    assert _phase1_state(abs_db) == _phase1_state(rel_db)


def test_missing_root_fails_fast_with_result_json(tmp_path: Path):
    proc = subprocess.run(
        [str(BINARY), "scan", "--root", str(tmp_path / "missing"),
         "--db", str(tmp_path / "x.db"), "--result-json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode != 0
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["outcome"] == "failed"
    assert any("root_unavailable" in error["message"] for error in result["errors"])
