#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stable compatibility entry point for the structural index scanner."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from index_state import (
    LockTimeoutError,
    RunStatus,
    ScanResult,
    StageError,
    project_scan_lock,
)
from migrations import (
    MIGRATION_HANDLERS,
    _migrate_v6_to_v7,
    _migrate_v7_to_v8,
    _migrate_v8_to_v9,
    _migrate_v9_to_v10,
    _migrate_v10_to_v11,
    _resolve_migration_path,
)
from scanner import (
    StructScanner,
    _compute_kind_hint,
    _resolve_git_head,
    scan_all,
    scan_files,
)
from schema import (
    SCHEMA_SQL,
    SUMMARY_STATUS_ENUM,
    VERSION,
    _transition_status,
)
from symbol_names import tokenize_symbol


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def _worker_config(cwd):
    remy_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
    if remy_src not in sys.path:
        sys.path.insert(0, remy_src)
    import remy_config

    config = remy_config.load_config(cwd, strict=False)
    secrets = [
        str(config.get(key))
        for key in remy_config.SECRET_KEYS
        if config.get(key) not in remy_config.INVALID_SECRET_VALUES
    ]
    return {
        "type": "worker_config",
        "schema_version": 1,
        "lock_timeout": config.get_float("REMY_INDEX_SCAN_LOCK_TIMEOUT"),
        "scan_timeout": config.get_int("REMY_STRUCT_SCAN_TIMEOUT"),
        "secret_values": secrets,
        "diagnostics": list(config.diagnostics),
    }


def _scan_result_json(result):
    return {
        "type": "scan_result",
        "schema_version": 1,
        "outcome": result.status.value,
        "successful_paths": list(result.successful_paths),
        "failed_paths": list(result.failed_paths),
        "deleted_paths": list(result.deleted_paths),
        "postprocess_complete": result.postprocess_complete,
        "errors": [
            {"stage": error.stage, "path": error.path, "message": error.message}
            for error in result.errors
        ],
    }


def _run_machine_scan(args):
    if not args.files:
        raise ValueError("--result-json requires one or more --files")
    lock = project_scan_lock(args.cwd, timeout=args.lock_timeout)
    try:
        lock.acquire()
        _print_json({"type": "progress", "stage": "lock_acquired"})
        result = scan_files(
            args.cwd,
            args.files,
            acquire_lock=False,
            manage_dirty=False,
        )
    except LockTimeoutError as exc:
        result = ScanResult.from_parts(
            failed_paths=args.files,
            errors=(StageError("scan_lock", str(exc)),),
            postprocess_complete=False,
        )
    finally:
        lock.release()
    _print_json(_scan_result_json(result))
    return result.exit_code


def main():
    import argparse

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Structural scan for logic_index.db")
    ap.add_argument("--files", nargs="*", help="Incremental: only scan these files")
    ap.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    ap.add_argument("--lock-timeout", type=float, default=None,
                    help="Override project scan lock wait in seconds")
    ap.add_argument("--consume-dirty", action="store_true",
                    help="Claim and acknowledge matching dirty queue entries")
    ap.add_argument("--result-json", action="store_true",
                    help="Emit the worker JSON Lines result contract")
    ap.add_argument("--worker-config-json", action="store_true",
                    help="Emit worker configuration as one JSON object")
    args = ap.parse_args()

    if args.worker_config_json:
        _print_json(_worker_config(args.cwd))
        return 0
    if args.result_json:
        try:
            return _run_machine_scan(args)
        except Exception as exc:
            _print_json({
                "type": "scan_result",
                "schema_version": 1,
                "outcome": "failed",
                "successful_paths": [],
                "failed_paths": sorted(set(args.files or ())),
                "deleted_paths": [],
                "postprocess_complete": False,
                "errors": [{"stage": "worker", "path": None, "message": str(exc)}],
            })
            return RunStatus.FAILED.exit_code

    try:
        if args.files:
            result = scan_files(
                args.cwd, args.files, lock_timeout=args.lock_timeout,
                manage_dirty=args.consume_dirty,
            )
        else:
            result = scan_all(
                args.cwd, lock_timeout=args.lock_timeout,
                manage_dirty=args.consume_dirty,
            )
    except LockTimeoutError as exc:
        print(f"Structural scan failed: {exc}", file=sys.stderr)
        return RunStatus.FAILED.exit_code

    for error in result.errors:
        location = f" ({error.path})" if error.path else ""
        print(f"[{error.stage}]{location} {error.message}", file=sys.stderr)
    print(f"STRUCT_SCAN_RESULT status={result.status.value} "
          f"successful={len(result.successful_paths)} failed={len(result.failed_paths)}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
