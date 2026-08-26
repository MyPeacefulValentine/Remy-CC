"""Regression tests for the fixed public TEE canary fixture."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tee_project_canary as canary
from oracle import bench as oracle_bench
from oracle import classification as oracle_classification
from oracle import comparator as oracle_comparator
from oracle import manifest as oracle_manifest
from oracle import normalization as oracle_normalization
from struct_scan import SCHEMA_SQL


@pytest.fixture
def fixture_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "tee"
    shutil.copytree(canary.FIXTURE_ROOT, destination)
    return destination


def _scan_fixture(root: Path, backend: str, validate: bool = True):
    manifest = canary.load_manifest(root / "manifest.json")
    if validate:
        canary.validate_fixture(root, manifest)
    (root / ".claude").mkdir(exist_ok=True)
    canary._write_scope_config(root, manifest, "product")
    context = canary.parser_backend(backend)
    actual_backend = context.__enter__()
    scanner = canary.StructScanner(str(root))
    return scanner, manifest, actual_backend, context


def _fresh_state(root: Path, backend: str, tmp_path: Path):
    destination = tmp_path / ("fresh-" + backend.replace("-", "_"))
    shutil.copytree(root, destination)
    shutil.rmtree(destination / ".claude", ignore_errors=True)
    scanner, _manifest, _actual, context = _scan_fixture(
        destination, backend, validate=False
    )
    try:
        result = scanner.scan_all()
        assert result.status.value == "success"
        assert result.postprocess_complete
        return canary.normalized_current_state(scanner.db)
    finally:
        scanner.db.close()
        context.__exit__(None, None, None)


def test_manifest_matches_fixed_upstream_blobs():
    manifest = canary.load_manifest()
    assert manifest["commit"] == "b11ffb19d83da42047cc0b5cbfbbfb95ba3304f4"
    assert manifest["license"] == "MulanPSL-2.0"
    canary.validate_fixture(canary.FIXTURE_ROOT, manifest)


def test_manifest_rejects_changed_fixture(fixture_copy: Path):
    target = fixture_copy / "framework/gtask/src/framework/tee_ns_cmd_dispatch.h"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(canary.CanaryError, match="blob mismatch"):
        canary.validate_fixture(
            fixture_copy, canary.load_manifest(fixture_copy / "manifest.json")
        )


def test_fixture_tree_sitter_canary():
    if not canary.c_cpp_parser.TREE_SITTER_AVAILABLE:
        pytest.skip("tree-sitter packages are unavailable")
    report = canary.run_canary(
        canary.FIXTURE_ROOT, backend="tree-sitter", fixture=True
    )
    assert report["parser_backend"] == "tree-sitter"
    assert {identity["backend"] for identity in report["parser_cache_identities"]} <= {
        "c-tree-sitter", "cpp-tree-sitter"
    }
    assert all(identity["environment"].get("tree-sitter") for identity in report["parser_cache_identities"])
    assert report["direct_edge_count"] > 0
    assert report["c_fnptr_dispatch_edge_count"] > 0
    assert report["idempotent_full_scan"] is True
    assert report["input_unchanged"] is True


def test_fixture_regex_canary():
    report = canary.run_canary(canary.FIXTURE_ROOT, backend="regex", fixture=True)
    assert report["parser_backend"] == "regex"
    assert report["parser_cache_identities"] == [{
        "contract_version": "2",
        "backend": "c-cpp-regex",
        "environment": {},
    }]
    assert report["direct_edge_count"] == 0
    assert report["c_fnptr_dispatch_edge_count"] > 0
    assert report["idempotent_full_scan"] is True
    assert report["input_unchanged"] is True


def test_canary_ignores_external_remy_db_path(tmp_path, monkeypatch):
    external = tmp_path / "outside" / "logic_index.db"
    monkeypatch.setenv("REMY_LOGIC_INDEX_DB_PATH", str(external))
    report = canary.run_canary(canary.FIXTURE_ROOT, backend="regex", fixture=True)
    assert report["status"] == "success"
    assert not external.exists()
    assert Path(canary.FIXTURE_ROOT).is_dir()


def test_incremental_handler_rename_matches_fresh_full_scan(
    fixture_copy: Path, tmp_path: Path
):
    backend = "tree-sitter" if canary.c_cpp_parser.TREE_SITTER_AVAILABLE else "regex"
    scanner, manifest, actual_backend, context = _scan_fixture(fixture_copy, backend)
    handler_path = Path("framework/gtask/src/app_load/tee_app_load_srv.c")
    dispatch_path = Path("framework/gtask/src/framework/tee_ns_cmd_dispatch.c")
    try:
        assert scanner.scan_all().status.value == "success"
        handler = fixture_copy / handler_path
        dispatch = fixture_copy / dispatch_path
        handler.write_text(
            handler.read_text(encoding="utf-8").replace(
                "need_load_app", "need_load_app_renamed"
            ),
            encoding="utf-8",
        )
        dispatch.write_text(
            dispatch.read_text(encoding="utf-8").replace(
                "need_load_app", "need_load_app_renamed"
            ),
            encoding="utf-8",
        )
        result = scanner.scan_files([handler_path.as_posix(), dispatch_path.as_posix()])
        assert result.status.value == "success"
        incremental = canary.normalized_current_state(scanner.db)
        assert scanner.db.execute(
            "SELECT 1 FROM edges WHERE callee='need_load_app' OR "
            "callee_qualified LIKE '%::need_load_app' LIMIT 1"
        ).fetchone() is None
        assert scanner.db.execute(
            "SELECT 1 FROM retrieval_documents WHERE node_ref LIKE '%::need_load_app'"
        ).fetchone() is None
        assert scanner.db.execute(
            "SELECT d.node_ref FROM retrieval_fts "
            "JOIN retrieval_documents d ON d.doc_id=retrieval_fts.rowid "
            "WHERE d.node_ref LIKE '%::need_load_app'"
        ).fetchone() is None
    finally:
        scanner.db.close()
        context.__exit__(None, None, None)

    fresh = _fresh_state(fixture_copy, actual_backend, tmp_path)
    assert incremental == fresh


def test_incremental_handler_delete_matches_fresh_full_scan(
    fixture_copy: Path, tmp_path: Path
):
    backend = "tree-sitter" if canary.c_cpp_parser.TREE_SITTER_AVAILABLE else "regex"
    scanner, _manifest, actual_backend, context = _scan_fixture(fixture_copy, backend)
    handler_path = Path("framework/gtask/src/app_load/tee_app_load_srv.c")
    dispatch_path = Path("framework/gtask/src/framework/tee_ns_cmd_dispatch.c")
    try:
        assert scanner.scan_all().status.value == "success"
        handler = fixture_copy / handler_path
        source = handler.read_text(encoding="utf-8")
        start = source.index("TEE_Result need_load_app(")
        end = source.index("\nstatic TEE_Result tee_cmd_params_parse", start)
        handler.write_text(source[:start] + source[end + 1 :], encoding="utf-8")
        dispatch = fixture_copy / dispatch_path
        dispatch.write_text(
            dispatch.read_text(encoding="utf-8").replace(
                "    { GLOBAL_CMD_ID_NEED_LOAD_APP,             need_load_app },\n\n",
                "",
            ),
            encoding="utf-8",
        )
        result = scanner.scan_files([dispatch_path.as_posix(), handler_path.as_posix()])
        assert result.status.value == "success"
        incremental = canary.normalized_current_state(scanner.db)
        assert scanner.db.execute(
            "SELECT 1 FROM edges WHERE callee='need_load_app' OR "
            "callee_qualified LIKE '%::need_load_app' LIMIT 1"
        ).fetchone() is None
        assert scanner.db.execute(
            "SELECT 1 FROM retrieval_documents WHERE node_ref LIKE '%::need_load_app'"
        ).fetchone() is None
    finally:
        scanner.db.close()
        context.__exit__(None, None, None)

    fresh = _fresh_state(fixture_copy, actual_backend, tmp_path)
    assert incremental == fresh


def test_incremental_file_order_is_commutative(fixture_copy: Path, tmp_path: Path):
    backend = "tree-sitter" if canary.c_cpp_parser.TREE_SITTER_AVAILABLE else "regex"
    states = []
    for name, order in (
        (
            "forward",
            [
                "framework/gtask/src/app_load/tee_app_load_srv.c",
                "framework/gtask/src/framework/tee_ns_cmd_dispatch.c",
            ],
        ),
        (
            "reverse",
            [
                "framework/gtask/src/framework/tee_ns_cmd_dispatch.c",
                "framework/gtask/src/app_load/tee_app_load_srv.c",
            ],
        ),
    ):
        root = tmp_path / name
        shutil.copytree(fixture_copy, root)
        scanner, _manifest, _actual, context = _scan_fixture(root, backend)
        try:
            assert scanner.scan_all().status.value == "success"
            for relative in order:
                path = root / relative
                path.write_text(
                    path.read_text(encoding="utf-8").replace(
                        "need_load_app", "need_load_app_renamed"
                    ),
                    encoding="utf-8",
                )
            assert scanner.scan_files(order).status.value == "success"
            states.append(canary.normalized_current_state(scanner.db))
        finally:
            scanner.db.close()
            context.__exit__(None, None, None)
    assert states[0] == states[1]


def test_cli_writes_json_report(tmp_path: Path):
    output = tmp_path / "report.json"
    exit_code = canary.main(
        [
            str(canary.FIXTURE_ROOT),
            "--fixture",
            "--backend",
            "regex",
            "--scope",
            "product",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    required = {
        "parser_backend",
        "parser_cache_identities",
        "scope",
        "source_commit",
        "revision_verified",
        "status",
        "postprocess_complete",
        "elapsed_seconds",
        "file_count",
        "symbol_count",
        "pattern_count",
        "pattern_type_counts",
        "pattern_sources",
        "direct_edge_count",
        "inferred_edge_count",
        "c_fnptr_dispatch_edge_count",
        "database_bytes",
        "wal_bytes",
    }
    assert required.issubset(report)
    assert report["pattern_count"] == sum(report["pattern_type_counts"].values())
    assert report["pattern_count"] == sum(
        source["count"] for source in report["pattern_sources"]
    )
    assert list(report["pattern_type_counts"]) == sorted(report["pattern_type_counts"])
    assert report["pattern_sources"] == sorted(
        report["pattern_sources"],
        key=lambda source: (
            -source["count"], source["file_path"], source["pattern_type"]
        ),
    )


_CANARY_LEGACY_COLUMNS = {
    "files": (
        "path", "struct_hash", "language", "layer", "imports", "kind_hint",
        "actual_kind", "parser_contract_version", "parser_backend",
        "parser_environment",
    ),
    "symbols": (
        "file_path", "name", "short_name", "type", "args", "lineno",
        "end_lineno", "hash", "bases", "name_tokens",
    ),
    "symbol_occurrences": (
        "file_path", "name", "occurrence_index", "type", "args", "lineno",
        "end_lineno", "hash", "is_canonical", "conflict_kind",
        "selection_reason",
    ),
    "edges": (
        "source_file", "caller", "callee", "callee_file", "callee_qualified",
        "line", "provenance", "synthesized_from", "via",
    ),
    "edge_candidates": (
        "source_file", "caller", "callee", "line", "candidate_qualified",
        "score",
    ),
    "patterns": (
        "file_path", "pattern_type", "signal_name", "handler", "line",
        "metadata",
    ),
    "clusters": ("name", "label", "entry_symbols", "file_count"),
    "cluster_members": ("cluster", "file_path"),
    "retrieval_documents": (
        "node_kind", "node_ref", "language", "symbol_type", "file_path",
        "name", "name_tokens", "signature", "summary_short", "summary_full",
        "content_hash",
    ),
}


def _seed_oracle_db(path: Path) -> None:
    db = sqlite3.connect(str(path))
    try:
        db.executescript(SCHEMA_SQL)
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer, imports) "
            "VALUES ('a.py','h1','python','Core','[\"b.py\"]')"
        )
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer, imports) "
            "VALUES ('b.py','h2','python','Util',NULL)"
        )
        db.execute(
            "INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) "
            "VALUES ('a.py','main','main','function','()',1,10,'hash-main',NULL,'main')"
        )
        db.execute(
            "INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) "
            "VALUES ('a.py','helper','helper','function','(x)',12,20,'hash-helper',NULL,'helper')"
        )
        db.execute(
            "INSERT INTO edges VALUES (NULL,'a.py','main','helper',NULL,'a.py::helper',3,'definite',NULL,NULL,'name')"
        )
        db.execute(
            "INSERT INTO edges VALUES (NULL,'a.py','helper','run','b.py','b.py::Util.run',14,'inferred',NULL,'interface-impl','name')"
        )
        db.execute(
            "INSERT INTO patterns VALUES (NULL,'a.py','django_signal_connect','post_save','on_save',8,NULL)"
        )
        db.execute(
            "INSERT INTO clusters (id,name,label,entry_symbols,file_count) "
            "VALUES (1,'core','Core','[\"a.py::main\"]',2)"
        )
        db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'a.py')")
        db.commit()
    finally:
        db.close()


def test_canary_view_stays_byte_compatible(tmp_path: Path):
    assert oracle_normalization.canary_columns() == _CANARY_LEGACY_COLUMNS
    db_path = tmp_path / "state.db"
    _seed_oracle_db(db_path)
    db = sqlite3.connect(str(db_path))
    try:
        assert canary.normalized_current_state(db) == oracle_normalization.canary_state(db)
    finally:
        db.close()


def test_oracle_view_extends_canary_view_as_suffix_only():
    oracle_columns = oracle_normalization.oracle_columns()
    for view, legacy in _CANARY_LEGACY_COLUMNS.items():
        excluded = oracle_normalization.CANARY_EXCLUDED_COLUMNS.get(view, ())
        assert oracle_columns[view] == legacy + tuple(excluded)


def test_comparator_is_reflexive(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _seed_oracle_db(db_path)
    findings = oracle_comparator.compare_dbs(db_path, db_path)
    assert findings == []


def test_comparator_detects_injected_mutations(tmp_path: Path):
    left_path = tmp_path / "left.db"
    right_path = tmp_path / "right.db"
    _seed_oracle_db(left_path)
    _seed_oracle_db(right_path)
    db = sqlite3.connect(str(right_path))
    try:
        db.execute("UPDATE symbols SET hash='mutated' WHERE name='main'")
        db.execute("DELETE FROM files WHERE path='b.py'")
        db.execute(
            "INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) "
            "VALUES ('a.py','extra','extra','function','()',30,32,'hash-extra',NULL,'extra')"
        )
        db.execute("DELETE FROM edges WHERE provenance='inferred'")
        db.commit()
    finally:
        db.close()

    findings = oracle_comparator.compare_dbs(left_path, right_path)
    categories = {(finding.view, finding.category) for finding in findings}
    assert ("symbols", oracle_comparator.CATEGORY_FIELD_MODIFIED) in categories
    assert ("files", oracle_comparator.CATEGORY_MISSING_ROW) in categories
    assert ("symbols", oracle_comparator.CATEGORY_EXTRA_ROW) in categories
    assert ("edges", oracle_comparator.CATEGORY_INFERRED_EDGE) in categories
    assert oracle_comparator.blocking(findings) == findings


def test_comparator_allowed_diff_columns_do_not_block(tmp_path: Path):
    left_path = tmp_path / "left.db"
    right_path = tmp_path / "right.db"
    for path, summary in ((left_path, "old text"), (right_path, "new text")):
        _seed_oracle_db(path)
        db = sqlite3.connect(str(path))
        try:
            db.execute(
                "INSERT INTO retrieval_documents "
                "(node_kind,node_ref,language,symbol_type,file_path,name,name_tokens,"
                "signature,summary_short,summary_full,content_hash,source_version,updated_at) "
                "VALUES ('symbol','a.py::main','python','function','a.py','main','main',"
                "'()',?,NULL,'ch',1,'2026-01-01T00:00:00')",
                (summary,),
            )
            db.commit()
        finally:
            db.close()

    findings = oracle_comparator.compare_dbs(left_path, right_path)
    assert [
        (finding.view, finding.category, finding.column) for finding in findings
    ] == [("retrieval_documents", oracle_comparator.CATEGORY_ALLOWED_DIFF, "summary_short")]
    assert oracle_comparator.blocking(findings) == []


def test_comparator_refuses_environment_mismatch():
    base = {
        "python_version": "3.12.9",
        "packages": {"tree-sitter": "0.25.2"},
        "registry": [{"language_id": "python", "extensions": [".py"], "cache_contract_version": "1"}],
        "schema_version": "12.0.0",
        "classification_version": "2",
        "comparator_version": "1",
        "fixtures": {"corpus/c/sample.c": "aa"},
    }
    other = dict(base, classification_version="1")
    with pytest.raises(oracle_comparator.EnvironmentMismatchError, match="classification_version"):
        oracle_comparator.ensure_same_environment(base, other)
    assert oracle_comparator.ensure_same_environment(
        base, other, allow_env_mismatch=True
    ) == ["classification_version"]
    with pytest.raises(oracle_comparator.EnvironmentMismatchError, match="fixtures"):
        oracle_comparator.ensure_same_environment(base, dict(base, fixtures={}))
    assert oracle_comparator.ensure_same_environment(base, dict(base)) == []


def test_environment_identity_ignores_producer_private_fields():
    base = {
        "registry": [],
        "schema_version": "12.0.0",
        "classification_version": "2",
        "comparator_version": "1",
        "fixtures": {},
        "python_version": "3.12.9",
        "packages": {"tree-sitter": "0.25.2"},
    }
    other = dict(
        base,
        python_version="3.10.0",
        packages={},
        producer={"implementation": "rust-scanner", "backend_versions": {}},
    )
    assert oracle_comparator.ensure_same_environment(base, other) == []


def test_oracle_manifest_generate_roundtrip(tmp_path: Path):
    manifest = oracle_manifest.generate()
    target = tmp_path / "oracle_manifest.json"
    oracle_manifest.write(manifest, target)
    loaded = oracle_manifest.load(target)
    assert loaded == manifest
    assert loaded["manifest_schema_version"] == 2
    assert loaded["producer"] == {
        "implementation": "python-oracle",
        "backend_versions": loaded["packages"],
    }
    identity = oracle_manifest.environment_identity(loaded)
    assert set(identity) == {
        "registry", "schema_version", "classification_version",
        "comparator_version", "fixtures",
    }
    registry = {entry["language_id"]: entry for entry in loaded["registry"]}
    assert set(registry) == {"PythonParser", "CCppParser", "TSParser", "RustParser"}
    assert registry["RustParser"]["cache_contract_version"] == "5"
    assert {gap["id"] for gap in loaded["known_gaps"]} == {
        "python-docstring-in-hash",
    }
    assert loaded["fixtures"], "oracle fixture corpus must be hashed"


def test_oracle_manifest_v1_upgrades_in_memory(tmp_path: Path):
    v1 = {
        "manifest_schema_version": 1,
        "generated_at": "2026-08-15T00:00:00+00:00",
        "commit": "78f6c312421435603c8407f597ab6ddee61e0f6b",
        "python_version": "3.12.9",
        "platform": "win32",
        "packages": {"tree-sitter": "0.25.2"},
        "registry": [],
        "schema_version": "12.0.0",
        "classification_version": "1",
        "comparator_version": "1",
        "config_snapshot": {},
        "fixtures": {},
        "known_gaps": [],
    }
    target = tmp_path / "v1_manifest.json"
    target.write_text(json.dumps(v1), encoding="utf-8")
    loaded = oracle_manifest.load(target)
    assert loaded["manifest_schema_version"] == 2
    assert loaded["producer"] == {
        "implementation": "python-oracle",
        "backend_versions": {"tree-sitter": "0.25.2"},
    }
    assert loaded["python_version"] == "3.12.9"
    with pytest.raises(ValueError, match="unsupported oracle manifest schema version"):
        oracle_manifest.validate(dict(v1, manifest_schema_version=3))


def test_phase1_views_are_ordered_column_subsets():
    assert set(oracle_classification.PHASE1_VIEWS) == {
        "files", "symbols", "symbol_occurrences", "edges", "patterns",
    }
    for view, spec in oracle_classification.PHASE1_VIEWS.items():
        full_spec = oracle_classification.VIEWS[view]
        full_columns = [column for column, _cls in full_spec["columns"]]
        subset = [column for column, _cls in spec["columns"]]
        positions = [full_columns.index(column) for column in subset]
        assert positions == sorted(positions)
        full_classes = dict(full_spec["columns"])
        assert all(cls == full_classes[column] for column, cls in spec["columns"])
    phase1_files = [c for c, _cls in oracle_classification.PHASE1_VIEWS["files"]["columns"]]
    assert "kind_hint" not in phase1_files
    assert "actual_kind" not in phase1_files
    phase1_edges = [c for c, _cls in oracle_classification.PHASE1_VIEWS["edges"]["columns"]]
    assert phase1_edges == ["source_file", "caller", "callee", "line", "call_form"]


def test_phase1_comparison_ignores_postprocess_differences(tmp_path: Path):
    left_path = tmp_path / "left.db"
    right_path = tmp_path / "right.db"
    _seed_oracle_db(left_path)
    _seed_oracle_db(right_path)
    db = sqlite3.connect(str(right_path))
    try:
        db.execute("DELETE FROM edges WHERE provenance='inferred'")
        db.execute("UPDATE files SET kind_hint='utility', actual_kind='utility' WHERE path='a.py'")
        db.execute("UPDATE edges SET callee_file=NULL, callee_qualified=NULL, provenance=NULL")
        db.execute("DELETE FROM cluster_members")
        db.execute("DELETE FROM clusters")
        db.commit()
    finally:
        db.close()

    full = oracle_comparator.compare_dbs(left_path, right_path)
    assert oracle_comparator.blocking(full) != []
    phase1 = oracle_comparator.compare_dbs(
        left_path,
        right_path,
        views=oracle_classification.PHASE1_VIEWS,
        row_filters=oracle_normalization.PHASE1_ROW_FILTERS,
    )
    assert phase1 == []


def test_phase1_comparison_still_detects_per_file_differences(tmp_path: Path):
    left_path = tmp_path / "left.db"
    right_path = tmp_path / "right.db"
    _seed_oracle_db(left_path)
    _seed_oracle_db(right_path)
    db = sqlite3.connect(str(right_path))
    try:
        db.execute("UPDATE symbols SET hash='mutated' WHERE name='main'")
        db.execute("UPDATE edges SET line=99 WHERE provenance='definite'")
        db.commit()
    finally:
        db.close()

    phase1 = oracle_comparator.compare_dbs(
        left_path,
        right_path,
        views=oracle_classification.PHASE1_VIEWS,
        row_filters=oracle_normalization.PHASE1_ROW_FILTERS,
    )
    categories = {(finding.view, finding.category) for finding in phase1}
    assert ("symbols", oracle_comparator.CATEGORY_FIELD_MODIFIED) in categories
    assert ("edges", oracle_comparator.CATEGORY_MISSING_ROW) in categories
    assert ("edges", oracle_comparator.CATEGORY_EXTRA_ROW) in categories
    assert oracle_comparator.blocking(phase1) == phase1


def test_bench_smoke_on_tee_fixture(tmp_path: Path):
    record = oracle_bench.measure_sample(
        canary.FIXTURE_ROOT, tmp_path / "bench", reps=1, k=1
    )
    assert record["file_count"] > 0
    assert record["full_scan_seconds_median"] > 0
    assert record["incremental_seconds_median"] > 0
    assert record["db_bytes_median"] > 0
    assert record["reps"] == 1 and record["k"] == 1
    if sys.platform == "win32":
        assert record["peak_commit_bytes_median"] > 10 * 1024 * 1024
