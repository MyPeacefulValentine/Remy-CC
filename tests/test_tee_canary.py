"""Regression tests for the fixed public TEE canary fixture."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tee_project_canary as canary


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
        "contract_version": "1",
        "backend": "c-cpp-regex",
        "environment": {},
    }]
    assert report["direct_edge_count"] == 0
    assert report["c_fnptr_dispatch_edge_count"] > 0
    assert report["idempotent_full_scan"] is True
    assert report["input_unchanged"] is True


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
