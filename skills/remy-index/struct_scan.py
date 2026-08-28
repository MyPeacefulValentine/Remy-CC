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
    lock = project_scan_lock(args.cwd, timeout=args.lock_timeout)
    try:
        lock.acquire()
        _print_json({"type": "progress", "stage": "lock_acquired"})
        if args.files:
            result = scan_files(args.cwd, args.files, acquire_lock=False)
        else:
            result = scan_all(args.cwd, acquire_lock=False)
    except LockTimeoutError as exc:
        result = ScanResult.from_parts(
            failed_paths=args.files or (),
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
    ap.add_argument("--result-json", action="store_true",
                    help="Emit the worker JSON Lines result contract")
    args = ap.parse_args()

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
            result = scan_files(args.cwd, args.files, lock_timeout=args.lock_timeout)
        else:
            result = scan_all(args.cwd, lock_timeout=args.lock_timeout)
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
