"""Deterministic candidate-level baseline for the current retrieval pipeline."""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

_EVAL_DIR = Path(__file__).resolve().parent
_REMY_ROOT = _EVAL_DIR.parent
_DEFAULT_TASKS = _EVAL_DIR / "tasks" / "retrieval_baseline" / "p1_1.json"
_DEFAULT_RESULTS = _EVAL_DIR / "results"
_SUPPORTED_FORMATS = frozenset(("1.0.0", "1.1.0"))
_CHANNELS: tuple[str, ...] = ("exact", "prefix", "bm25", "fuzzy")
_DETERMINISTIC_CHANNELS: tuple[str, ...] = ("exact", "prefix", "bm25")


def _load_index_modules():
    for relative in ("remy-src", "skills/remy-index"):
        path = str(_REMY_ROOT / relative)
        if path not in sys.path:
            sys.path.insert(0, path)
    index_mcp_search = importlib.import_module("index_mcp_search")
    index_mcp_navigate = importlib.import_module("index_mcp_navigate")
    index_mcp_common = importlib.import_module("index_mcp_common")
    retrieval_projection = importlib.import_module("retrieval_projection")
    schema = importlib.import_module("schema")
    symbol_names = importlib.import_module("symbol_names")
    return (
        index_mcp_search,
        index_mcp_navigate,
        index_mcp_common,
        retrieval_projection,
        schema.SCHEMA_SQL,
        schema.VERSION,
        symbol_names.tokenize_symbol,
    )


def load_spec(path: Path) -> dict:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or spec.get("format_version") not in _SUPPORTED_FORMATS:
        raise ValueError(
            "retrieval baseline spec must use format_version 1.0.0 or 1.1.0"
        )
    fixture = spec.get("fixture")
    tasks = spec.get("tasks")
    if not isinstance(fixture, dict) or not isinstance(tasks, list) or not tasks:
        raise ValueError("retrieval baseline spec requires fixture and non-empty tasks")

    symbols = fixture.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("fixture.symbols must be a non-empty list")
    node_refs = {f"{row['file_path']}::{row['name']}" for row in symbols}
    if len(node_refs) != len(symbols):
        raise ValueError("fixture symbols must have unique file_path/name identities")

    task_ids = set()
    for task in tasks:
        task_id = task.get("id")
        expected = task.get("expected_nodes")
        if not isinstance(task_id, str) or not task_id or task_id in task_ids:
            raise ValueError("task ids must be non-empty and unique")
        task_ids.add(task_id)
        if not isinstance(expected, list) or any(ref not in node_refs for ref in expected):
            raise ValueError(f"task {task_id} references a symbol outside the fixture")
        expected_error = task.get("expected_error", False)
        if not isinstance(expected_error, bool):
            raise ValueError(f"task {task_id} expected_error must be boolean")
        expected_empty = task.get("expected_empty")
        if not isinstance(expected_empty, bool):
            raise ValueError(f"task {task_id} expected_empty must be boolean")
        if not expected_error and expected_empty != (len(expected) == 0):
            raise ValueError(f"task {task_id} has inconsistent expected_empty")
        if expected_error and expected:
            raise ValueError(f"task {task_id} error tasks cannot declare candidates")
        if not isinstance(task.get("query"), str):
            raise ValueError(f"task {task_id} query must be a string")
        limit = task.get("limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"task {task_id} limit must be an integer")
        if spec["format_version"] == "1.0.0" and limit <= 0:
            raise ValueError(f"task {task_id} limit must be positive")
    return spec


def build_fixture(spec: dict, db_path: Path) -> sqlite3.Connection:
    _, _, _, projection, schema_sql, version, tokenize_symbol = _load_index_modules()
    db = sqlite3.connect(str(db_path))
    db.executescript(schema_sql)
    db.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (version,))

    fixture = spec["fixture"]
    files = fixture.get("files", [])
    for index, row in enumerate(files):
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer, imports) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["path"],
                f"fixture-{index}",
                row.get("language"),
                row.get("layer", "Core"),
                json.dumps(row.get("imports", [])),
            ),
        )

    now = "2026-08-01T00:00:00"
    for row in fixture["symbols"]:
        name = row["name"]
        short_name = row.get("short_name") or name.replace("::", ".").rsplit(".", 1)[-1]
        db.execute(
            "INSERT INTO symbols "
            "(file_path, name, short_name, type, args, lineno, end_lineno, "
            "hash, bases, name_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["file_path"],
                name,
                short_name,
                row.get("type", "function"),
                row.get("args"),
                row.get("lineno", 1),
                row.get("end_lineno", row.get("lineno", 1)),
                row.get("hash"),
                json.dumps(row.get("bases")) if row.get("bases") else None,
                tokenize_symbol(name),
            ),
        )
        if row.get("summary_short"):
            summary = {
                "short": row["summary_short"],
                "full": row.get("summary_full"),
            }
            db.execute(
                "INSERT INTO summary_versions "
                "(node_kind, node_ref, version, summary, status, created_at) "
                "VALUES ('symbol', ?, 1, ?, 'ok', ?)",
                (f"{row['file_path']}::{name}", json.dumps(summary), now),
            )

    file_summaries = fixture.get("file_summaries", {})
    for file_path, short in file_summaries.items():
        db.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('file', ?, 1, ?, 'ok', ?)",
            (file_path, json.dumps({"short": short, "full": None}), now),
        )

    for cluster in fixture.get("clusters", []):
        members = cluster.get("members", [])
        db.execute(
            "INSERT INTO clusters (name, label, entry_symbols, file_count) "
            "VALUES (?, ?, ?, ?)",
            (
                cluster["name"],
                cluster.get("label"),
                json.dumps(cluster.get("entry_symbols", [])),
                len(members),
            ),
        )
        cluster_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for file_path in members:
            db.execute(
                "INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, ?)",
                (cluster_id, file_path),
            )
        if cluster.get("summary_short"):
            db.execute(
                "INSERT INTO summary_versions "
                "(node_kind, node_ref, version, summary, status, created_at) "
                "VALUES ('cluster', ?, 1, ?, 'ok', ?)",
                (
                    cluster["name"],
                    json.dumps({"short": cluster["summary_short"], "full": None}),
                    now,
                ),
            )

    projection.rebuild_projection(db)
    db.commit()
    return db


def _candidate_rows(rows: list[tuple], channel: str) -> list[dict]:
    candidates = []
    for rank, (name, file_path, line, symbol_type, score) in enumerate(rows, 1):
        candidates.append({
            "channel": channel,
            "rank": rank,
            "node_ref": f"{file_path}::{name}",
            "name": name,
            "file_path": file_path,
            "line": line,
            "symbol_type": symbol_type,
            "score": score,
        })
    return candidates


def _nearest_rank(samples: list[int], percentile: float) -> int:
    if not samples:
        raise ValueError("cannot calculate a percentile from no samples")
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def measure(call: Callable[[], object], warmups: int, iterations: int) -> dict:
    if warmups < 0 or iterations <= 0:
        raise ValueError("warmups must be non-negative and iterations must be positive")
    for _ in range(warmups):
        call()
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        call()
        samples.append(time.perf_counter_ns() - start)
    return {
        "unit": "ns",
        "warmups": warmups,
        "iterations": iterations,
        "samples": samples,
        "min": min(samples),
        "p50": _nearest_rank(samples, 0.50),
        "p95": _nearest_rank(samples, 0.95),
        "max": max(samples),
    }


def score_ranked(candidates: list[dict], expected_nodes: list[str]) -> dict:
    expected = set(expected_nodes)
    refs = [row["node_ref"] for row in candidates]
    if not expected:
        return {
            "eligible": False,
            "recall_at_1": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "reciprocal_rank": None,
        }

    def recall_at(k: int) -> float:
        return len(expected.intersection(refs[:k])) / len(expected)

    first = next((rank for rank, ref in enumerate(refs, 1) if ref in expected), None)
    return {
        "eligible": True,
        "recall_at_1": recall_at(1),
        "recall_at_5": recall_at(5),
        "recall_at_10": recall_at(10),
        "reciprocal_rank": 0.0 if first is None else 1.0 / first,
    }


def aggregate_metrics(task_records: list[dict]) -> dict:
    eligible = [row["score"] for row in task_records if row["score"]["eligible"]]
    empty_tasks = [
        row for row in task_records
        if row["expected_empty"] and not row.get("expected_error", False)
    ]
    error_tasks = [row for row in task_records if row.get("expected_error", False)]
    successful = [row for row in task_records if not row.get("actual_error", False)]
    actual_empty = [row for row in successful if not row["selected_candidates"]]

    def mean(field: str) -> float | None:
        if not eligible:
            return None
        return sum(row[field] for row in eligible) / len(eligible)

    empty_correct = sum(not row["selected_candidates"] for row in empty_tasks)
    error_correct = sum(row.get("actual_error", False) for row in error_tasks)
    return {
        "eligible_task_count": len(eligible),
        "recall_at_1": mean("recall_at_1"),
        "recall_at_5": mean("recall_at_5"),
        "recall_at_10": mean("recall_at_10"),
        "mrr": mean("reciprocal_rank"),
        "actual_no_result_rate": (
            len(actual_empty) / len(successful) if successful else None
        ),
        "expected_empty_task_count": len(empty_tasks),
        "expected_empty_accuracy": (
            empty_correct / len(empty_tasks) if empty_tasks else None
        ),
        "expected_error_task_count": len(error_tasks),
        "expected_error_accuracy": (
            error_correct / len(error_tasks) if error_tasks else None
        ),
    }


CandidateRow = tuple[str, str, int | None, str, float]
ChannelCall = Callable[[], list[CandidateRow]]


def _task_arguments(task: dict) -> dict:
    return {
        "match": task.get("match", "all"),
        "language": task.get("language", ""),
        "symbol_type": task.get("symbol_type", ""),
        "path_hint": task.get("path_hint", ""),
    }


def _channel_calls(queries: Any, db: sqlite3.Connection,
                   task: dict) -> tuple[dict[str, ChannelCall], Callable[[], str],
                                        dict, dict]:
    text = task["query"]
    limit = task.get("limit", 10)
    file_hint = task.get("file_hint", "")
    arguments = _task_arguments(task)
    diagnostics: dict = {}
    query = queries._make_search_query(
        text, limit, file_hint, **arguments
    )
    channels: dict[str, ChannelCall] = {
        "exact": lambda: cast(list[CandidateRow], queries._search_exact(db, query)),
        "prefix": lambda: cast(list[CandidateRow], queries._search_like(db, query)),
        "bm25": lambda: cast(list[CandidateRow], queries._search_fts(
            db, query, diagnostics=diagnostics
        )),
        "fuzzy": lambda: cast(list[CandidateRow], queries._search_fuzzy(db, query)),
    }
    public = lambda: cast(str, queries.query_search_impl(
        text, limit, file_hint, **arguments
    ))
    return channels, public, diagnostics, arguments


def _capture_channel(call: ChannelCall) -> tuple[list[CandidateRow], dict]:
    try:
        rows = call()
    except sqlite3.Error as error:
        return [], {
            "status": "channel_error",
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
    return rows, {"status": "ok" if rows else "no_match"}


def _safe_measure_call(call: Callable[[], object]) -> Callable[[], object]:
    def invoke():
        try:
            return call()
        except sqlite3.Error:
            return None
    return invoke


def evaluate_task(queries, common, db, db_path: Path, task: dict,
                  warmups: int, iterations: int) -> dict:
    text = task["query"]
    limit = task.get("limit", 10)
    file_hint = task.get("file_hint", "")
    arguments = _task_arguments(task)
    public_call = lambda: cast(str, queries.query_search_impl(
        text, limit, file_hint, **arguments
    ))
    diagnostics: dict = {}
    channels: dict[str, list[dict]] = {name: [] for name in _CHANNELS}
    raw_rows: dict[str, list[CandidateRow]] = {name: [] for name in _CHANNELS}
    channel_status = {
        name: {"status": "not_applicable"} for name in _CHANNELS
    }
    channel_calls: dict[str, ChannelCall] = {}

    try:
        channel_calls, public_call, diagnostics, arguments = _channel_calls(
            queries, db, task
        )
    except queries._SearchInputError:
        pass
    else:
        for channel in _CHANNELS:
            rows, status = _capture_channel(channel_calls[channel])
            raw_rows[channel] = rows
            channels[channel] = _candidate_rows(rows, channel)
            channel_status[channel] = status

    merged = queries._merge_candidates(
        [(name, raw_rows[name]) for name in _DETERMINISTIC_CHANNELS], limit
    )
    if merged:
        selected = "union"
    elif raw_rows["fuzzy"]:
        merged = queries._merge_candidates([("fuzzy", raw_rows["fuzzy"])], limit)
        selected = "fuzzy"
    else:
        selected = None
    selected_candidates = [
        {
            "rank": rank,
            "node_ref": f"{item['file_path']}::{item['name']}",
            "name": item["name"],
            "file_path": item["file_path"],
            "line": item["lineno"],
            "symbol_type": item["symbol_type"],
            "priority": item["priority"],
            "sources": [
                {"channel": channel, "rank": source_rank}
                for channel, source_rank in item["sources"]
            ],
        }
        for rank, item in enumerate(merged, 1)
    ]
    with common.database_override(db_path):
        public_output = public_call()
        timed_calls: dict[str, Callable[[], object]] = {
            name: _safe_measure_call(call) for name, call in channel_calls.items()
        }
        timed_calls["public"] = public_call
        timings = {
            name: measure(call, warmups, iterations)
            for name, call in timed_calls.items()
        }

    expected_channel = task.get("expected_channel")
    expected_error = task.get("expected_error", False)
    actual_error = public_output.startswith("Error:")
    score = score_ranked(selected_candidates, task["expected_nodes"])
    if expected_error:
        score = {
            "eligible": False,
            "recall_at_1": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "reciprocal_rank": None,
        }
    return {
        "id": task["id"],
        "scenario": task.get("scenario", task["id"]),
        "query": text,
        "limit": limit,
        "file_hint": file_hint,
        **arguments,
        "expected_nodes": task["expected_nodes"],
        "expected_empty": task["expected_empty"],
        "expected_error": expected_error,
        "expected_channel": expected_channel,
        "actual_channel": selected,
        "actual_error": actual_error,
        "error_matches_expectation": expected_error == actual_error,
        "channel_matches_expectation": expected_channel == selected,
        "channel_status": channel_status,
        "channel_diagnostics": {"bm25": diagnostics},
        "channels": channels,
        "selected_candidates": selected_candidates,
        "public_output": public_output,
        "score": score,
        "timings": timings,
    }


def _navigation_intents(spec: dict) -> list[str]:
    block = spec.get("navigation", {})
    intents = block.get("intents")
    if isinstance(intents, list) and intents:
        return [str(item) for item in intents]
    return [str(block.get("intent", "retrieval baseline"))]


def _corpus_candidates(clusters: list[dict], files: list[dict]) -> list[dict]:
    candidates = []
    for cluster in clusters:
        candidates.append({
            "kind": "cluster",
            "cluster": cluster["name"],
            "file": None,
            "symbol": None,
            "short": cluster["short"] or cluster["label"] or "",
        })
    for row in files:
        if row["short"]:
            candidates.append({
                "kind": "file",
                "cluster": "(corpus)",
                "file": row["path"],
                "symbol": None,
                "short": row["short"],
            })
    return candidates


def navigation_measurement(queries, db_path: Path | None, intents: list[str], top_k: int) -> dict:
    if db_path is None:
        return {"measured": False, "reason": "navigate database not provided"}
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"navigate database not found: {path}")
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        clusters, files = queries._collect_navigate_corpus(db)
        corpus_candidates = _corpus_candidates(clusters, files)
        records = []
        for intent in intents:
            corpus_chars = len(
                queries._build_navigate_prompt(intent, corpus_candidates, top_k)
            )
            candidates = queries._navigate_candidates(db, intent)
            counts = {"cluster": 0, "file": 0, "symbol": 0}
            for entry in candidates:
                counts[entry["kind"]] += 1
            if candidates:
                prompt_chars = len(
                    queries._build_navigate_prompt(intent, candidates, top_k)
                )
                fallback_reason = None
                fallback_prompt_chars = None
            else:
                prompt_chars = 0
                fallback_reason = "lexical_empty"
                fallback_prompt_chars = len(queries._build_navigate_prompt(
                    intent, queries._cluster_fallback_candidates(db, clusters), top_k
                ))
            records.append({
                "intent": intent,
                "top_k": top_k,
                "corpus_chars": corpus_chars,
                "candidate_counts": counts,
                "candidate_total": len(candidates),
                "prompt_chars": prompt_chars,
                "llm_called": False,
                "fallback_reason": fallback_reason,
                "fallback_prompt_chars": fallback_prompt_chars,
                "candidates_key": queries._navigate_cache_key(intent, top_k, candidates),
            })
        return {
            "measured": True,
            "database": str(path),
            "top_k": top_k,
            "corpus": {
                "cluster_count": len(clusters),
                "file_count": len(files),
                "file_with_short_count": sum(bool(row.get("short")) for row in files),
            },
            "intents": records,
        }
    finally:
        db.close()


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(_REMY_ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _database_sizes(db_path: Path) -> dict:
    wal_path = Path(str(db_path) + "-wal")
    return {
        "database_bytes": db_path.stat().st_size,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
    }


def compare_results(current: dict, baseline: dict) -> dict:
    baseline_tasks = {task["id"]: task for task in baseline.get("tasks", [])}
    changes = []
    for task in current.get("tasks", []):
        previous = baseline_tasks.get(task["id"])
        if previous is None:
            changes.append({"id": task["id"], "classification": "new_task"})
            continue
        previous_refs = [row["node_ref"] for row in previous.get("selected_candidates", [])]
        current_refs = [row["node_ref"] for row in task.get("selected_candidates", [])]
        changed = (
            previous.get("actual_channel") != task.get("actual_channel")
            or previous_refs != current_refs
            or previous.get("public_output") != task.get("public_output")
        )
        if task["id"] == "empty_query" and task.get("actual_error"):
            classification = "intentional_contract_change"
        elif changed:
            classification = "behavior_change"
        else:
            classification = "unchanged"
        changes.append({
            "id": task["id"],
            "classification": classification,
            "before_channel": previous.get("actual_channel"),
            "after_channel": task.get("actual_channel"),
            "before_candidates": previous_refs,
            "after_candidates": current_refs,
        })
    excluded_ids = {
        row["id"] for row in changes
        if row["classification"] == "intentional_contract_change"
    }
    comparable_current = [
        task for task in current.get("tasks", []) if task["id"] not in excluded_ids
    ]
    comparable_baseline = [
        task for task in baseline.get("tasks", []) if task["id"] not in excluded_ids
    ]
    return {
        "baseline_spec_id": baseline.get("meta", {}).get("spec_id"),
        "baseline_git_commit": baseline.get("meta", {}).get("git_commit"),
        "metrics_before_raw": baseline.get("metrics"),
        "metrics_after_raw": current.get("metrics"),
        "metrics_before": aggregate_metrics(comparable_baseline),
        "metrics_after": aggregate_metrics(comparable_current),
        "excluded_contract_change_ids": sorted(excluded_ids),
        "database_before": baseline.get("database"),
        "database_after": current.get("database"),
        "tasks": changes,
    }


def run_baseline(spec: dict, *, warmups: int = 3, iterations: int = 30,
                 navigate_db: Path | None = None) -> dict:
    queries, navigate, common, _, _, schema_version, _ = _load_index_modules()
    with tempfile.TemporaryDirectory(prefix="remy-retrieval-baseline-") as tmp:
        db_path = Path(tmp) / "logic_index.db"
        db = build_fixture(spec, db_path)
        try:
            before_version = db.execute(
                "SELECT value FROM meta WHERE key='version'"
            ).fetchone()[0]
            before_migrations = db.execute(
                "SELECT COUNT(*) FROM migration_log"
            ).fetchone()[0]
            task_records = [
                evaluate_task(queries, common, db, db_path, task, warmups, iterations)
                for task in spec["tasks"]
            ]
            after_version = db.execute(
                "SELECT value FROM meta WHERE key='version'"
            ).fetchone()[0]
            after_migrations = db.execute(
                "SELECT COUNT(*) FROM migration_log"
            ).fetchone()[0]
            sizes = _database_sizes(db_path)
        finally:
            db.close()

    return {
        "format_version": spec.get("format_version", "1.0.0"),
        "meta": {
            "suite_version": (_REMY_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            "git_commit": _git_commit(),
            "schema_version": schema_version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "parser_backend": "synthetic-fixture",
            "parser_configuration": "not-applicable",
            "warmups": warmups,
            "iterations": iterations,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "spec_id": spec.get("id"),
        },
        "database": {
            **sizes,
            "version_before": before_version,
            "version_after": after_version,
            "migration_count_before": before_migrations,
            "migration_count_after": after_migrations,
        },
        "navigation": navigation_measurement(
            navigate,
            navigate_db,
            _navigation_intents(spec),
            spec.get("navigation", {}).get("top_k", 5),
        ),
        "tasks": task_records,
        "metrics": aggregate_metrics(task_records),
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        "remy-eval retrieval-baseline",
        description="Measure the current deterministic candidate retrieval pipeline.",
    )
    parser.add_argument("--tasks", type=Path, default=_DEFAULT_TASKS)
    parser.add_argument("--navigate-db", type=Path, default=None)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--update-snapshot", type=Path, default=None)
    parser.add_argument("--compare-baseline", type=Path, default=None)
    parser.add_argument("--comparison-output", type=Path, default=None)
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    spec = load_spec(args.tasks)
    result = run_baseline(
        spec,
        warmups=args.warmups,
        iterations=args.iterations,
        navigate_db=args.navigate_db,
    )

    written = []
    if args.output:
        _atomic_write_json(args.output, result)
        written.append(args.output)
    if args.save:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        output = _DEFAULT_RESULTS / run_id / "retrieval_baseline.json"
        if output.parent.exists():
            raise FileExistsError(f"result directory already exists: {output.parent}")
        _atomic_write_json(output, result)
        written.append(output)
    if args.update_snapshot:
        _atomic_write_json(args.update_snapshot, result)
        written.append(args.update_snapshot)
    comparison = None
    if args.compare_baseline:
        baseline = json.loads(args.compare_baseline.read_text(encoding="utf-8"))
        comparison = compare_results(result, baseline)
        if args.comparison_output:
            _atomic_write_json(args.comparison_output, {
                "format_version": "1.1.0",
                "result": result,
                "comparison": comparison,
            })
            written.append(args.comparison_output)
    elif args.comparison_output:
        raise ValueError("--comparison-output requires --compare-baseline")

    print(json.dumps({
        "spec_id": result["meta"]["spec_id"],
        "task_count": len(result["tasks"]),
        "metrics": result["metrics"],
        "comparison": comparison,
        "written": [str(path) for path in written],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
