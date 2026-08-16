"""Table-level normalized comparator between two logic index databases.

Compares oracle-view projections (normalization.oracle_state) under the
declarative field classification. Blocking findings are field
modifications, missing rows, extra rows, and inferred-edge differences;
allowed_diff columns are reported as informational findings only.

Comparison is refused when the two oracle manifests describe different
generation environments, unless explicitly overridden for diagnosis.

Any change to the finding semantics is a change of the oracle identity
and MUST bump COMPARATOR_VERSION.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from . import classification, manifest, normalization

COMPARATOR_VERSION = "1"

CATEGORY_MISSING_ROW = "missing_row"
CATEGORY_EXTRA_ROW = "extra_row"
CATEGORY_FIELD_MODIFIED = "field_modified"
CATEGORY_INFERRED_EDGE = "inferred_edge"
CATEGORY_ALLOWED_DIFF = "allowed_diff"

BLOCKING_CATEGORIES = frozenset(
    {
        CATEGORY_MISSING_ROW,
        CATEGORY_EXTRA_ROW,
        CATEGORY_FIELD_MODIFIED,
        CATEGORY_INFERRED_EDGE,
    }
)


class EnvironmentMismatchError(RuntimeError):
    """Raised when the two manifests describe different environments."""


@dataclass(frozen=True)
class Finding:
    view: str
    category: str
    key: tuple
    column: Optional[str] = None
    left: Any = None
    right: Any = None


def ensure_same_environment(
    left_manifest: dict, right_manifest: dict, allow_env_mismatch: bool = False
) -> list[str]:
    left_identity = manifest.environment_identity(left_manifest)
    right_identity = manifest.environment_identity(right_manifest)
    mismatched = sorted(
        field
        for field in set(left_identity) | set(right_identity)
        if left_identity.get(field) != right_identity.get(field)
    )
    if mismatched and not allow_env_mismatch:
        raise EnvironmentMismatchError(
            "manifest environment identity differs: " + ", ".join(mismatched)
        )
    return mismatched


def _edge_category(view: str, columns: tuple, row: tuple, base_category: str) -> str:
    if view != "edges" or "provenance" not in columns:
        return base_category
    provenance = row[columns.index("provenance")]
    synthesized_from = row[columns.index("synthesized_from")]
    if provenance == "inferred" or synthesized_from is not None:
        return CATEGORY_INFERRED_EDGE
    return base_category


def compare_states(
    left: dict, right: dict, views: Optional[dict] = None
) -> list[Finding]:
    findings: list[Finding] = []
    for view, spec in (views or classification.VIEWS).items():
        columns = tuple(column for column, _cls in spec["columns"])
        classes = classification.column_classes(view)
        key = spec["key"]
        left_rows = left[view]
        right_rows = right[view]
        if key is None:
            findings.extend(
                _compare_multiset(view, columns, left_rows, right_rows)
            )
            continue
        key_indexes = tuple(columns.index(column) for column in key)
        left_map = {tuple(row[i] for i in key_indexes): row for row in left_rows}
        right_map = {tuple(row[i] for i in key_indexes): row for row in right_rows}
        for row_key in sorted(set(left_map) - set(right_map), key=repr):
            findings.append(Finding(view, CATEGORY_MISSING_ROW, row_key, left=left_map[row_key]))
        for row_key in sorted(set(right_map) - set(left_map), key=repr):
            findings.append(Finding(view, CATEGORY_EXTRA_ROW, row_key, right=right_map[row_key]))
        for row_key in sorted(set(left_map) & set(right_map), key=repr):
            left_row = left_map[row_key]
            right_row = right_map[row_key]
            for index, column in enumerate(columns):
                if column in key or left_row[index] == right_row[index]:
                    continue
                category = (
                    CATEGORY_FIELD_MODIFIED
                    if classes[column] == classification.EXACT
                    else CATEGORY_ALLOWED_DIFF
                )
                findings.append(
                    Finding(view, category, row_key, column, left_row[index], right_row[index])
                )
    return findings


def _compare_multiset(
    view: str, columns: tuple, left_rows: list, right_rows: list
) -> list[Finding]:
    from collections import Counter

    left_counts = Counter(tuple(row) for row in left_rows)
    right_counts = Counter(tuple(row) for row in right_rows)
    findings: list[Finding] = []
    for row in sorted((left_counts - right_counts).elements(), key=repr):
        category = _edge_category(view, columns, row, CATEGORY_MISSING_ROW)
        findings.append(Finding(view, category, row, left=row))
    for row in sorted((right_counts - left_counts).elements(), key=repr):
        category = _edge_category(view, columns, row, CATEGORY_EXTRA_ROW)
        findings.append(Finding(view, category, row, right=row))
    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.category in BLOCKING_CATEGORIES]


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def compare_dbs(
    left_path: Path,
    right_path: Path,
    left_manifest: Optional[dict] = None,
    right_manifest: Optional[dict] = None,
    allow_env_mismatch: bool = False,
    views: Optional[dict] = None,
    row_filters: Optional[dict] = None,
) -> list[Finding]:
    if left_manifest is not None and right_manifest is not None:
        ensure_same_environment(left_manifest, right_manifest, allow_env_mismatch)
    view_columns = {
        view: tuple(column for column, _cls in spec["columns"])
        for view, spec in (views or classification.VIEWS).items()
    }
    left_db = _open_readonly(left_path)
    try:
        left_state = normalization.state(left_db, view_columns, row_filters)
    finally:
        left_db.close()
    right_db = _open_readonly(right_path)
    try:
        right_state = normalization.state(right_db, view_columns, row_filters)
    finally:
        right_db.close()
    return compare_states(left_state, right_state, views)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--manifest-left", type=Path)
    parser.add_argument("--manifest-right", type=Path)
    parser.add_argument("--allow-env-mismatch", action="store_true")
    parser.add_argument(
        "--phase1",
        action="store_true",
        help="compare only the phase-1 per-file fact subset "
        "(classification.PHASE1_VIEWS, synthesized edges excluded)",
    )
    args = parser.parse_args(argv)

    left_manifest = manifest.load(args.manifest_left) if args.manifest_left else None
    right_manifest = manifest.load(args.manifest_right) if args.manifest_right else None
    views = classification.PHASE1_VIEWS if args.phase1 else None
    row_filters = normalization.PHASE1_ROW_FILTERS if args.phase1 else None
    try:
        findings = compare_dbs(
            args.left,
            args.right,
            left_manifest,
            right_manifest,
            allow_env_mismatch=args.allow_env_mismatch,
            views=views,
            row_filters=row_filters,
        )
    except EnvironmentMismatchError as exc:
        print(f"ORACLE_COMPARE status=env_mismatch error={exc}", file=sys.stderr)
        return 3
    print(json.dumps([asdict(finding) for finding in findings], ensure_ascii=False, indent=2))
    blocked = blocking(findings)
    print(
        f"ORACLE_COMPARE status={'diff' if blocked else 'equal'} "
        f"views={'phase1' if args.phase1 else 'full'} "
        f"blocking={len(blocked)} total={len(findings)}"
    )
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
