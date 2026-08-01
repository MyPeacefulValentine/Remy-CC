#!/usr/bin/env python
"""Offline regression canary for a fixed public TEE project revision."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Generator
import zipfile

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
INDEX_DIR = REPO_ROOT / "skills" / "remy-index"
FIXTURE_ROOT = TESTS_DIR / "fixtures" / "tee_canary"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

sys.path.insert(0, str(INDEX_DIR))

import parsers.c_cpp_parser as c_cpp_parser
from struct_scan import StructScanner


class CanaryError(RuntimeError):
    """Raised when a canary precondition or assertion fails."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != 1:
        raise CanaryError("Unsupported TEE canary manifest schema")
    return manifest


def git_blob_sha(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def validate_fixture(root: Path, manifest: dict[str, Any]) -> None:
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.is_file():
            raise CanaryError(f"Fixture file is missing: {relative}")
        actual = git_blob_sha(path.read_bytes())
        if actual != expected:
            raise CanaryError(
                f"Fixture blob mismatch for {relative}: expected {expected}, got {actual}"
            )


def verify_git_revision(root: Path, expected: str) -> None:
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise CanaryError(f"TEE project is not a readable Git repository: {root}") from exc
    if actual != expected:
        raise CanaryError(
            f"TEE project revision mismatch: expected {expected}, got {actual}"
        )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _materialize_project(
    source: Path,
    destination: Path,
    manifest: dict[str, Any],
    fixture: bool,
) -> None:
    if fixture:
        validate_fixture(source, manifest)
        shutil.copytree(source, destination)
        return

    verify_git_revision(source, manifest["commit"])
    try:
        archive = subprocess.check_output(
            ["git", "-C", str(source), "archive", "--format=zip", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise CanaryError("Unable to archive the fixed TEE project revision") from exc
    destination.mkdir(parents=True)
    archive_path = destination.parent / "source.zip"
    archive_path.write_bytes(archive)
    with zipfile.ZipFile(archive_path) as bundle:
        bundle.extractall(destination)


def _write_scope_config(
    root: Path, manifest: dict[str, Any], scope: str
) -> None:
    rules = manifest["scope_rules"][scope]
    config_dir = root / ".claude"
    config_dir.mkdir(exist_ok=True)
    content = "\n".join(f"!{rule}" for rule in rules) + "\n"
    (config_dir / "logic_index_config").write_text(content, encoding="utf-8")


@contextlib.contextmanager
def parser_backend(requested: str) -> Generator[str, None, None]:
    previous = c_cpp_parser.TREE_SITTER_AVAILABLE
    if requested == "tree-sitter":
        if not previous:
            raise CanaryError("tree-sitter backend requested but packages are unavailable")
        actual = "tree-sitter"
    elif requested == "regex":
        c_cpp_parser.TREE_SITTER_AVAILABLE = False
        actual = "regex"
    else:
        actual = "tree-sitter" if previous else "regex"
    try:
        yield actual
    finally:
        c_cpp_parser.TREE_SITTER_AVAILABLE = previous


def normalized_current_state(db: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "files": db.execute(
            "SELECT path,struct_hash,language,layer,imports,kind_hint,actual_kind "
            "FROM files ORDER BY path"
        ).fetchall(),
        "symbols": db.execute(
            "SELECT file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens "
            "FROM symbols ORDER BY file_path,name"
        ).fetchall(),
        "symbol_occurrences": db.execute(
            "SELECT file_path,name,occurrence_index,type,args,lineno,end_lineno,hash,"
            "is_canonical,conflict_kind,selection_reason FROM symbol_occurrences "
            "ORDER BY file_path,name,occurrence_index"
        ).fetchall(),
        "edges": db.execute(
            "SELECT source_file,caller,callee,callee_file,callee_qualified,line,provenance,"
            "synthesized_from,via FROM edges ORDER BY source_file,caller,callee,"
            "callee_qualified,line,provenance,via"
        ).fetchall(),
        "edge_candidates": db.execute(
            "SELECT e.source_file,e.caller,e.callee,e.line,ec.candidate_qualified,ec.score "
            "FROM edge_candidates ec JOIN edges e ON e.id=ec.edge_id "
            "ORDER BY e.source_file,e.caller,e.callee,e.line,ec.candidate_qualified"
        ).fetchall(),
        "patterns": db.execute(
            "SELECT file_path,pattern_type,signal_name,handler,line,metadata FROM patterns "
            "ORDER BY file_path,pattern_type,signal_name,handler,line,metadata"
        ).fetchall(),
        "clusters": db.execute(
            "SELECT name,label,entry_symbols,file_count FROM clusters ORDER BY name"
        ).fetchall(),
        "cluster_members": db.execute(
            "SELECT c.name,cm.file_path FROM cluster_members cm "
            "JOIN clusters c ON c.id=cm.cluster_id ORDER BY c.name,cm.file_path"
        ).fetchall(),
        "retrieval_documents": db.execute(
            "SELECT node_kind,node_ref,language,symbol_type,file_path,name,name_tokens,"
            "signature,summary_short,summary_full,content_hash FROM retrieval_documents "
            "ORDER BY node_kind,node_ref"
        ).fetchall(),
    }


def assert_required_facts(
    db: sqlite3.Connection,
    manifest: dict[str, Any],
    backend: str,
) -> None:
    symbols = {row[0] for row in db.execute("SELECT name FROM symbols").fetchall()}
    missing = set(manifest["required_symbols"]) - symbols
    if missing:
        raise CanaryError(f"Required symbols are missing: {sorted(missing)}")

    pattern = manifest["required_pattern"]
    row = db.execute(
        "SELECT 1 FROM patterns WHERE pattern_type=? AND signal_name=? LIMIT 1",
        (pattern["pattern_type"], pattern["signal_name"]),
    ).fetchone()
    if row is None:
        raise CanaryError("Required function-pointer typedef fact is missing")

    inferred = manifest["required_inferred_edge"]
    row = db.execute(
        "SELECT provenance,via FROM edges WHERE source_file=? AND caller=? "
        "AND callee_qualified=?",
        (
            inferred["source_file"],
            inferred["caller"],
            inferred["callee_qualified"],
        ),
    ).fetchone()
    if row != (inferred["provenance"], inferred["via"]):
        raise CanaryError(f"Required inferred edge is missing or mislabeled: {row}")

    if backend == "tree-sitter":
        direct = manifest["required_direct_edge"]
        row = db.execute(
            "SELECT provenance FROM edges WHERE source_file=? AND caller=? "
            "AND callee_qualified=?",
            (
                direct["source_file"],
                direct["caller"],
                direct["callee_qualified"],
            ),
        ).fetchone()
        if row is None:
            raise CanaryError("Required direct edge is missing")

    duplicate = db.execute(
        "SELECT source_file,caller,callee_qualified,via,COUNT(*) FROM edges "
        "WHERE provenance='inferred' GROUP BY source_file,caller,callee_qualified,via "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise CanaryError(f"Duplicate inferred edge identity found: {duplicate}")


def collect_report(
    scanner: StructScanner,
    manifest: dict[str, Any],
    backend: str,
    scope: str,
    elapsed: float,
    revision_verified: bool,
    idempotent: bool,
) -> dict[str, Any]:
    db = scanner.db
    database_bytes = Path(scanner.db_path).stat().st_size
    wal_path = Path(scanner.db_path + "-wal")
    return {
        "parser_backend": backend,
        "scope": scope,
        "source_commit": manifest["commit"],
        "revision_verified": revision_verified,
        "status": "success",
        "postprocess_complete": True,
        "idempotent_full_scan": idempotent,
        "elapsed_seconds": round(elapsed, 6),
        "file_count": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "symbol_count": db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "pattern_count": db.execute("SELECT COUNT(*) FROM patterns").fetchone()[0],
        "direct_edge_count": db.execute(
            "SELECT COUNT(*) FROM edges WHERE provenance IS NULL OR provenance!='inferred'"
        ).fetchone()[0],
        "inferred_edge_count": db.execute(
            "SELECT COUNT(*) FROM edges WHERE provenance='inferred'"
        ).fetchone()[0],
        "c_fnptr_dispatch_edge_count": db.execute(
            "SELECT COUNT(*) FROM edges WHERE via='c-fnptr-dispatch'"
        ).fetchone()[0],
        "database_bytes": database_bytes,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
    }


def run_canary(
    project: Path,
    *,
    backend: str = "auto",
    scope: str = "product",
    fixture: bool = False,
    repeat_full_scan: bool = True,
) -> dict[str, Any]:
    project = project.resolve()
    manifest = load_manifest(project / "manifest.json" if fixture else MANIFEST_PATH)
    before = _tree_digest(project)

    with tempfile.TemporaryDirectory(prefix="remy-tee-canary-") as temp_dir:
        scan_root = Path(temp_dir) / "project"
        _materialize_project(project, scan_root, manifest, fixture)
        _write_scope_config(scan_root, manifest, scope)

        old_db_path = os.environ.pop("LOGIC_INDEX_DB_PATH", None)
        try:
            with parser_backend(backend) as actual_backend:
                scanner = StructScanner(str(scan_root))
                try:
                    start = time.perf_counter()
                    first_result = scanner.scan_all()
                    elapsed = time.perf_counter() - start
                    if (
                        first_result.status.value != "success"
                        or not first_result.postprocess_complete
                    ):
                        raise CanaryError(
                            f"Initial scan failed: status={first_result.status.value}, "
                            f"errors={first_result.errors}"
                        )
                    assert_required_facts(scanner.db, manifest, actual_backend)
                    first_state = normalized_current_state(scanner.db)
                    idempotent = True
                    if repeat_full_scan:
                        second_result = scanner.scan_all()
                        if (
                            second_result.status.value != "success"
                            or not second_result.postprocess_complete
                        ):
                            raise CanaryError(
                                f"Repeated scan failed: status={second_result.status.value}, "
                                f"errors={second_result.errors}"
                            )
                        second_state = normalized_current_state(scanner.db)
                        idempotent = first_state == second_state
                        if not idempotent:
                            raise CanaryError("Repeated full scan changed normalized state")
                    report = collect_report(
                        scanner,
                        manifest,
                        actual_backend,
                        scope,
                        elapsed,
                        True,
                        idempotent,
                    )
                finally:
                    scanner.db.close()
        finally:
            if old_db_path is not None:
                os.environ["LOGIC_INDEX_DB_PATH"] = old_db_path

    after = _tree_digest(project)
    if before != after:
        raise CanaryError("Canary modified the input project")
    report["input_unchanged"] = True
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument(
        "--backend", choices=("auto", "tree-sitter", "regex"), default="auto"
    )
    parser.add_argument(
        "--scope", choices=("product", "full-tree"), default="product"
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_canary(
            args.project,
            backend=args.backend,
            scope=args.scope,
            fixture=args.fixture,
        )
    except (CanaryError, OSError, sqlite3.Error) as exc:
        print(f"TEE_CANARY_RESULT status=failed error={exc}", file=sys.stderr)
        return 1

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
