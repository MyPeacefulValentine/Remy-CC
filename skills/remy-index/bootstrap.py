"""Bootstrap entry for hierarchical summaries.

Populates ``summary_versions`` for files and clusters that lack a
``status='ok'`` row, reusing the NULL-as-pending pattern: failed nodes
remain unsummarized and are selected again on the next invocation.

Mode resolution (``SUMMARY_BOOTSTRAP_MODE`` env, default ``auto``):
- ``auto`` runs unattended unless ``OPENAI_API_KEY`` is unset or the
  project exceeds ``BOOTSTRAP_AUTO_SIZE_GUARD`` files (downgrades to
  ``ask``).
- ``ask`` skips execution and returns a ``needs_user_confirmation`` flag.
- ``never`` skips entirely.
"""
import concurrent.futures
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import summarizer
from constants import DEFAULT_MAX_WORKERS, DB_BUSY_TIMEOUT_MS, DB_CONNECT_TIMEOUT_S


VALID_MODES = ("auto", "ask", "never")


def _env_int(name, default):
    try:
        value = os.environ.get(name)
        return int(value if value is not None else default)
    except (ValueError, TypeError):
        return default


def needs_bootstrap(db):
    file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    summarized_files = db.execute(
        "SELECT COUNT(DISTINCT node_ref) FROM summary_versions "
        "WHERE status='ok' AND node_kind='file'"
    ).fetchone()[0]
    if summarized_files < file_count:
        return True
    cluster_count = db.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
    summarized_clusters = db.execute(
        "SELECT COUNT(DISTINCT node_ref) FROM summary_versions "
        "WHERE status='ok' AND node_kind='cluster'"
    ).fetchone()[0]
    return summarized_clusters < cluster_count


def resolve_mode(db):
    requested = os.environ.get("SUMMARY_BOOTSTRAP_MODE", "auto").lower()
    if requested not in VALID_MODES:
        requested = "auto"
    if requested == "auto":
        if not os.environ.get("OPENAI_API_KEY"):
            return "ask"
        file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        guard = _env_int("BOOTSTRAP_AUTO_SIZE_GUARD", 500)
        if file_count > guard:
            return "ask"
    return requested


def _pending_files(db):
    return [r[0] for r in db.execute(
        """SELECT path FROM files
           WHERE NOT EXISTS (
               SELECT 1 FROM summary_versions sv
               WHERE sv.node_kind='file' AND sv.node_ref = files.path AND sv.status='ok'
           )"""
    ).fetchall()]


def _pending_clusters(db):
    return [r[0] for r in db.execute(
        """SELECT name FROM clusters
           WHERE NOT EXISTS (
               SELECT 1 FROM summary_versions sv
               WHERE sv.node_kind='cluster' AND sv.node_ref = clusters.name AND sv.status='ok'
           )"""
    ).fetchall()]


def _run_file_layer(db, llm_call, concurrency):
    pending = _pending_files(db)
    if not pending:
        print("[bootstrap] file layer: 0 pending, skipped", flush=True)
        return {"requested": 0, "done": 0, "failed": 0}
    print(f"[bootstrap] file layer: {len(pending)} pending, concurrency={concurrency}", flush=True)
    hint_map = {
        path: hint
        for path, hint in db.execute(
            "SELECT path, kind_hint FROM files WHERE path IN ("
            + ",".join(["?"] * len(pending))
            + ")",
            pending,
        ).fetchall()
    }
    done = 0
    if concurrency <= 1:
        for fp in pending:
            try:
                payload, status = summarizer.summarize_file(db, fp, hint_map.get(fp), llm_call)
            except Exception:
                payload, status = None, "pending"
            print(f"  file [{done + 1}/{len(pending)}] {fp} -> {status}", flush=True)
            if payload is None and status == "pending":
                continue
            summarizer.write_summary_version(db, "file", fp, payload, status)
            if status == "ok":
                done += 1
        print(f"[bootstrap] file layer done: {done}/{len(pending)} ok", flush=True)
        return {"requested": len(pending), "done": done, "failed": len(pending) - done}

    db_path = _db_path(db)
    results = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        def task(fp):
            worker_db = sqlite3.connect(db_path, timeout=DB_CONNECT_TIMEOUT_S)
            worker_db.execute("PRAGMA journal_mode=WAL")
            worker_db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
            try:
                return fp, summarizer.summarize_file(worker_db, fp, hint_map.get(fp), llm_call)
            finally:
                worker_db.close()
        futures = [executor.submit(task, fp) for fp in pending]
        for fut in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                fp, (payload, status) = fut.result()
            except Exception as exc:
                print(f"  file [{completed}/{len(pending)}] worker_error: {type(exc).__name__}", flush=True)
                continue
            print(f"  file [{completed}/{len(pending)}] {fp} -> {status}", flush=True)
            results.append((fp, payload, status))
    for fp, payload, status in results:
        if payload is None and status == "pending":
            continue
        summarizer.write_summary_version(db, "file", fp, payload, status)
        if status == "ok":
            done += 1
    print(f"[bootstrap] file layer done: {done}/{len(pending)} ok", flush=True)
    return {"requested": len(pending), "done": done, "failed": len(pending) - done}


def _run_cluster_layer(db, llm_call, concurrency):
    pending = _pending_clusters(db)
    if not pending:
        print("[bootstrap] cluster layer: 0 pending, skipped", flush=True)
        return {"requested": 0, "done": 0, "failed": 0}
    print(f"[bootstrap] cluster layer: {len(pending)} pending, concurrency={concurrency}", flush=True)
    done = 0
    if concurrency <= 1:
        for name in pending:
            try:
                payload, status = summarizer.summarize_cluster(db, name, llm_call)
            except Exception:
                payload, status = None, "pending"
            print(f"  cluster [{done + 1}/{len(pending)}] {name} -> {status}", flush=True)
            if payload is None and status == "pending":
                continue
            summarizer.write_summary_version(db, "cluster", name, payload, status)
            if status == "ok":
                done += 1
        print(f"[bootstrap] cluster layer done: {done}/{len(pending)} ok", flush=True)
        return {"requested": len(pending), "done": done, "failed": len(pending) - done}

    db_path = _db_path(db)
    results = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        def task(name):
            worker_db = sqlite3.connect(db_path, timeout=DB_CONNECT_TIMEOUT_S)
            worker_db.execute("PRAGMA journal_mode=WAL")
            worker_db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
            try:
                return name, summarizer.summarize_cluster(worker_db, name, llm_call)
            finally:
                worker_db.close()
        futures = [executor.submit(task, name) for name in pending]
        for fut in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                name, (payload, status) = fut.result()
            except Exception as exc:
                print(f"  cluster [{completed}/{len(pending)}] worker_error: {type(exc).__name__}", flush=True)
                continue
            print(f"  cluster [{completed}/{len(pending)}] {name} -> {status}", flush=True)
            results.append((name, payload, status))
    for name, payload, status in results:
        if payload is None and status == "pending":
            continue
        summarizer.write_summary_version(db, "cluster", name, payload, status)
        if status == "ok":
            done += 1
    print(f"[bootstrap] cluster layer done: {done}/{len(pending)} ok", flush=True)
    return {"requested": len(pending), "done": done, "failed": len(pending) - done}


def _db_path(db):
    row = db.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


def bootstrap_summaries(db, llm_call, mode=None):
    if mode is None:
        mode = resolve_mode(db)
    if mode == "never":
        return {
            "mode": "never", "file_done": 0, "cluster_done": 0,
            "file_requested": 0, "cluster_requested": 0,
            "file_failed": 0, "cluster_failed": 0, "skipped": True,
        }
    if mode == "ask":
        pending_files = len(_pending_files(db))
        pending_clusters = len(_pending_clusters(db))
        return {
            "mode": "ask",
            "file_done": 0,
            "cluster_done": 0,
            "file_requested": 0,
            "cluster_requested": 0,
            "file_failed": 0,
            "cluster_failed": 0,
            "skipped": True,
            "needs_user_confirmation": True,
            "pending_files": pending_files,
            "pending_clusters": pending_clusters,
        }
    concurrency = _env_int("OPENAI_MAX_WORKERS", DEFAULT_MAX_WORKERS)
    file_result = _run_file_layer(db, llm_call, concurrency)
    cluster_result = _run_cluster_layer(db, llm_call, concurrency)
    return {
        "mode": mode,
        "file_done": file_result["done"],
        "cluster_done": cluster_result["done"],
        "file_requested": file_result["requested"],
        "cluster_requested": cluster_result["requested"],
        "file_failed": file_result["failed"],
        "cluster_failed": cluster_result["failed"],
        "skipped": False,
    }
