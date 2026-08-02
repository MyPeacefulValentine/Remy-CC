#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Stable compatibility entry point for the structural index scanner."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from index_state import LockTimeoutError, RunStatus
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


if __name__ == "__main__":
    import argparse

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description="Structural scan for logic_index.db")
    ap.add_argument("--files", nargs="*", help="Incremental: only scan these files")
    ap.add_argument("--cwd", default=os.getcwd(), help="Project root directory")
    ap.add_argument("--lock-timeout", type=float, default=None,
                    help="Override project scan lock wait in seconds")
    ap.add_argument("--consume-dirty", action="store_true",
                    help="Claim and acknowledge matching dirty queue entries")
    args = ap.parse_args()

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
        sys.exit(RunStatus.FAILED.exit_code)

    for error in result.errors:
        location = f" ({error.path})" if error.path else ""
        print(f"[{error.stage}]{location} {error.message}", file=sys.stderr)
    print(f"STRUCT_SCAN_RESULT status={result.status.value} "
          f"successful={len(result.successful_paths)} failed={len(result.failed_paths)}")
    sys.exit(result.exit_code)
