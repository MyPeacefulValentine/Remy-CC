"""Propagation service for hierarchical summaries.

Extracted from run.py (A1.1). Owns force-recompute checks, counter resets,
candidate collection, child-change payload assembly, parent rewrite, and the
file/cluster propagation pass. Functions take an open SQLite connection;
LLM access is injected as an LlmClient-compatible object or a bare callable.
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retrieval_projection import (
    AVAILABLE_SUMMARY_STATUSES,
    has_current_summary,
    select_current_summary,
)

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config


def _env_int(name, default):
    key = name if name.startswith("REMY_") else "REMY_" + name
    try:
        return remy_config.load_config(strict=True).get_int(key)
    except (KeyError, TypeError, remy_config.ConfigError):
        return default


def force_recompute_check(db, parent_kind, parent_ref):
    """Return True when THRESHOLD_PRIMARY / THRESHOLD_BACKUP / INTERVAL_DAYS fires."""
    if not db:
        return False
    row = db.execute(
        "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
        "FROM node_change_counters WHERE node_kind = ? AND node_ref = ?",
        (parent_kind, parent_ref),
    ).fetchone()
    if not row:
        return False
    child_cnt, leaf_cnt, last_force = row
    threshold_primary = _env_int("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", 50)
    threshold_backup = _env_int("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", -1)
    interval_days = _env_int("REMY_FORCE_RECOMPUTE_INTERVAL_DAYS", 30)
    if threshold_primary > 0 and child_cnt >= threshold_primary:
        return True
    if threshold_backup >= 0 and leaf_cnt >= threshold_backup:
        return True
    if last_force and interval_days > 0:
        try:
            from datetime import timedelta
            elapsed = datetime.now() - datetime.fromisoformat(last_force)
            if elapsed >= timedelta(days=interval_days):
                return True
        except (ValueError, TypeError):
            pass
    return False


def zero_counter(db, parent_kind, parent_ref, mark_force=False):
    """Reset child_change_count and leaf_descendant_count for a node."""
    if not db:
        return
    if mark_force:
        db.execute(
            "UPDATE node_change_counters SET child_change_count = 0, "
            "leaf_descendant_count = 0, last_force_recompute_at = ? "
            "WHERE node_kind = ? AND node_ref = ?",
            (datetime.now().isoformat(timespec='seconds'), parent_kind, parent_ref),
        )
    else:
        db.execute(
            "UPDATE node_change_counters SET child_change_count = 0, "
            "leaf_descendant_count = 0 "
            "WHERE node_kind = ? AND node_ref = ?",
            (parent_kind, parent_ref),
        )
    db.commit()


def collect_propagation_candidates(db, parent_kind):
    """Return parents with a current summary and child_change_count > 0."""
    if not db:
        return []
    rows = db.execute(
        "SELECT node_ref, child_change_count FROM node_change_counters "
        "WHERE node_kind = ? AND child_change_count > 0",
        (parent_kind,),
    ).fetchall()
    return [
        (node_ref, count)
        for node_ref, count in rows
        if has_current_summary(db, parent_kind, node_ref)
    ]


def get_latest_ok_summary(db, node_kind, node_ref):
    """Return the current usable summary payload, or None."""
    if not db:
        return None
    current = select_current_summary(db, node_kind, node_ref)
    if current.get("id") is None:
        return None
    return {"short": current.get("short"), "full": current.get("full")}


def build_child_changes_payload(db, parent_kind, parent_ref):
    """Assemble {child_ref, old_summary, new_summary} list for judge_propagation.

    Children are determined structurally:
        parent_kind='file'    -> children = symbols in that file
        parent_kind='cluster' -> children = files in that cluster
    old_summary is the greatest version below the current one regardless of that
    version's status, so a predecessor already marked ``stale`` still serves as
    the comparison baseline.
    """
    if not db:
        return []
    if parent_kind == "file":
        rows = db.execute(
            "SELECT name FROM symbols WHERE file_path = ?", (parent_ref,)
        ).fetchall()
        child_kind = "symbol"
        child_refs = [f"{parent_ref}::{r[0]}" for r in rows]
    elif parent_kind == "cluster":
        rows = db.execute(
            """SELECT cm.file_path FROM cluster_members cm
               JOIN clusters c ON cm.cluster_id = c.id
               WHERE c.name = ?""",
            (parent_ref,),
        ).fetchall()
        child_kind = "file"
        child_refs = [r[0] for r in rows]
    else:
        return []

    changes = []
    for child_ref in child_refs:
        current = select_current_summary(db, child_kind, child_ref)
        if current.get("id") is None:
            continue
        new_summary = {
            "short": current.get("short"),
            "full": current.get("full"),
        }
        previous_rows = db.execute(
            "SELECT summary FROM summary_versions "
            "WHERE node_kind = ? AND node_ref = ? AND version < ? "
            "ORDER BY version DESC LIMIT 1",
            (child_kind, child_ref, current["version"]),
        ).fetchall()
        old_summary = None
        if previous_rows and previous_rows[0][0]:
            try:
                old_summary = json.loads(previous_rows[0][0])
            except (json.JSONDecodeError, TypeError):
                old_summary = None
        if new_summary == old_summary:
            continue
        changes.append({
            "child_ref": child_ref,
            "old_summary": old_summary,
            "new_summary": new_summary,
        })
    return changes


def rewrite_parent_summary(db, parent_kind, parent_ref, llm_call):
    """Regenerate a parent summary and return whether an ok version was written."""
    if not db:
        return False
    try:
        import summarizer
    except ImportError as exc:
        print(f"Warning: summarizer unavailable ({exc}); cannot rewrite {parent_kind} {parent_ref}.")
        return False
    if parent_kind == "file":
        row = db.execute(
            "SELECT kind_hint FROM files WHERE path = ?", (parent_ref,)
        ).fetchone()
        hint = row[0] if row else None
        payload, status = summarizer.summarize_file(db, parent_ref, hint, llm_call)
    elif parent_kind == "cluster":
        payload, status = summarizer.summarize_cluster(db, parent_ref, llm_call)
    else:
        return False
    if payload is None or status not in AVAILABLE_SUMMARY_STATUSES:
        return False
    summarizer.write_summary_version(
        db, parent_kind, parent_ref, payload, status
    )
    return True


def run_propagation_pass(db, llm_client):
    """Run propagation judgment for file then cluster level.

    For each candidate (parent with ok summary AND child_change_count > 0):
    - If force-recompute fires: rewrite parent + zero counter + stamp last_force.
    - Else if no child summary text changed: zero counter (nothing to accumulate).
    - Else: call judge_propagation; propagate=true → rewrite + zero counter,
      propagate=false → keep counter (accumulates toward THRESHOLD_PRIMARY).
    """
    if not db or llm_client.circuit_open or not llm_client.api_key:
        return None
    print("\n[run] entering propagation pass...", flush=True)
    try:
        from llm_judge import judge_propagation
    except ImportError as exc:
        print(f"Warning: llm_judge unavailable ({exc}); skipping propagation pass.")
        return None

    stats = {
        "file_force": 0, "file_propagate": 0, "file_skip": 0,
        "cluster_force": 0, "cluster_propagate": 0, "cluster_skip": 0,
        "errors": 0,
    }
    for parent_kind in ("file", "cluster"):
        candidates = collect_propagation_candidates(db, parent_kind)
        for parent_ref, _child_cnt in candidates:
            if llm_client.circuit_open:
                break
            if force_recompute_check(db, parent_kind, parent_ref):
                if rewrite_parent_summary(db, parent_kind, parent_ref, llm_client.call):
                    zero_counter(db, parent_kind, parent_ref, mark_force=True)
                    stats[f"{parent_kind}_force"] += 1
                else:
                    stats[f"{parent_kind}_skip"] += 1
                    stats["errors"] += 1
                continue
            parent_prev = get_latest_ok_summary(db, parent_kind, parent_ref)
            child_changes = build_child_changes_payload(db, parent_kind, parent_ref)
            if not child_changes:
                zero_counter(db, parent_kind, parent_ref)
                stats[f"{parent_kind}_skip"] += 1
                continue
            try:
                verdict = judge_propagation(
                    db, parent_kind, parent_ref, parent_prev,
                    child_changes, llm_client.call,
                )
            except Exception as exc:
                print(f"Error judging {parent_kind} {parent_ref}: {exc}")
                stats[f"{parent_kind}_skip"] += 1
                stats["errors"] += 1
                continue
            if verdict.get("propagate"):
                if rewrite_parent_summary(db, parent_kind, parent_ref, llm_client.call):
                    zero_counter(db, parent_kind, parent_ref)
                    stats[f"{parent_kind}_propagate"] += 1
                else:
                    stats[f"{parent_kind}_skip"] += 1
                    stats["errors"] += 1
            else:
                stats[f"{parent_kind}_skip"] += 1

    print("\n=== Propagation Pass ===")
    print(
        "PROPAGATION_RESULT "
        f"file_propagate={stats['file_propagate']} file_skip={stats['file_skip']} "
        f"file_force={stats['file_force']} "
        f"cluster_propagate={stats['cluster_propagate']} cluster_skip={stats['cluster_skip']} "
        f"cluster_force={stats['cluster_force']}"
    )
    print("=" * 25)
    return stats
