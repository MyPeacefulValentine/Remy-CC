#!/usr/bin/env python3
"""Delegated Python CLI: configuration UI and summary maintenance.

The remy-cc binary owns the full command surface and spawns this module
for the config and summary families only (the summary runtime and config
UI stay Python-owned).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remy_config


def _user_home() -> Path:
    try:
        return Path.home()
    except RuntimeError:
        value = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if not value:
            print("Cannot determine home directory. Set $HOME and retry.", file=sys.stderr)
            raise SystemExit(1)
        return Path(value)


def get_claude_home() -> Path:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value) if value else _user_home() / ".claude"


def _load_config_ui():
    script = Path(__file__).resolve().parent / "config_ui.py"
    if not script.exists():
        print("Error: config_ui.py not found at " + str(script), file=sys.stderr)
        sys.exit(1)
    import importlib.util
    spec = importlib.util.spec_from_file_location("config_ui", str(script))
    if spec is None or spec.loader is None:
        print("Error: unable to load config_ui.py at " + str(script), file=sys.stderr)
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_config(args):
    if args.path:
        project_dir = Path(args.path).resolve()
        if not project_dir.is_dir():
            print("Error: directory not found: " + str(project_dir), file=sys.stderr)
            sys.exit(1)
        _load_config_ui().main(mode="project", target_path=str(project_dir))
    else:
        _load_config_ui().main()


def _load_summary_modules():
    skill_dir = get_claude_home() / "skills" / "remy-index"
    if not skill_dir.is_dir():
        repo_skill_dir = Path(__file__).resolve().parent.parent / "skills" / "remy-index"
        if repo_skill_dir.is_dir():
            skill_dir = repo_skill_dir
        else:
            print("Error: skills/remy-index not found in ~/.claude or repo.", file=sys.stderr)
            sys.exit(1)
    sys.path.insert(0, str(skill_dir))
    import importlib
    modules = {}
    for name in ("bootstrap", "summarizer", "llm_judge", "index_state", "retrieval_projection"):
        try:
            modules[name] = importlib.import_module(name)
        except ImportError as exc:
            print("Error importing {}: {}".format(name, exc), file=sys.stderr)
            sys.exit(1)
    return modules, skill_dir


def _open_logic_db(cwd):
    import sqlite3
    snapshot = remy_config.load_config(cwd, strict=True)
    db_path = Path(str(snapshot.get("REMY_LOGIC_INDEX_DB_PATH")))
    if not db_path.exists():
        print("Error: logic_index.db not found at {}. Run /remy-index first.".format(db_path), file=sys.stderr)
        sys.exit(1)
    db = sqlite3.connect(str(db_path), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _default_llm_call():
    skill_dir = get_claude_home() / "skills" / "remy-index"
    if not skill_dir.is_dir():
        skill_dir = Path(__file__).resolve().parent.parent / "skills" / "remy-index"
    sys.path.insert(0, str(skill_dir))
    import importlib
    llm_mod = importlib.import_module("llm_client")

    def _call(prompt):
        return llm_mod.LlmClient().call(prompt)

    return _call


def cmd_summary_rebuild(args):
    cwd = Path(args.path).resolve() if args.path else Path.cwd()
    if not cwd.is_dir():
        print("Error: directory not found: " + str(cwd), file=sys.stderr)
        sys.exit(1)
    os.chdir(str(cwd))
    modules, _ = _load_summary_modules()
    bootstrap = modules["bootstrap"]
    summarizer = modules["summarizer"]
    lock = modules["index_state"].project_scan_lock(str(cwd))
    lock.acquire()
    try:
        llm_call = _default_llm_call()
        db = _open_logic_db(cwd)
    except BaseException:
        lock.release()
        raise
    try:
        if args.node_kind and args.node_ref:
            if args.node_kind == "file":
                hint_row = db.execute("SELECT kind_hint FROM files WHERE path = ?", (args.node_ref,)).fetchone()
                hint = hint_row[0] if hint_row else None
                payload, status = summarizer.summarize_file(db, args.node_ref, hint, llm_call)
            elif args.node_kind == "cluster":
                payload, status = summarizer.summarize_cluster(db, args.node_ref, llm_call)
            else:
                print("Error: --node-kind must be 'file' or 'cluster'.", file=sys.stderr)
                sys.exit(2)
            if payload is None and status == "pending":
                print("LLM unavailable; status='pending'. No new version written.")
                return
            version = summarizer.write_summary_version(db, args.node_kind, args.node_ref, payload, status)
            print("Rewrote {}::{} -> version {} (status={}).".format(args.node_kind, args.node_ref, version, status))
            return

        mode = args.mode if args.mode else None
        result = bootstrap.bootstrap_summaries(db, llm_call, mode=mode)
        print("Bootstrap summary: mode={mode} file_done={file_done} cluster_done={cluster_done} skipped={skipped}".format(**result))
        if result.get("needs_user_confirmation"):
            print("  Pending files: {} / Pending clusters: {}".format(
                result.get("pending_files", 0), result.get("pending_clusters", 0)))
    finally:
        db.close()
        lock.release()


def cmd_summary_vacuum(args):
    cwd = Path(args.path).resolve() if args.path else Path.cwd()
    if not cwd.is_dir():
        print("Error: directory not found: " + str(cwd), file=sys.stderr)
        sys.exit(1)
    modules, _ = _load_summary_modules()
    lock = modules["index_state"].project_scan_lock(str(cwd))
    projection = modules["retrieval_projection"]
    lock.acquire()
    try:
        db = _open_logic_db(cwd)
    except BaseException:
        lock.release()
        raise
    try:
        from datetime import datetime, timedelta
        days = args.older_than
        if days <= 0:
            print("Error: --older-than must be > 0.", file=sys.stderr)
            sys.exit(2)
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        before = db.execute("SELECT COUNT(*) FROM judge_cache").fetchone()[0]
        db.execute("DELETE FROM judge_cache WHERE created_at < ?", (cutoff,))
        db.commit()
        after = db.execute("SELECT COUNT(*) FROM judge_cache").fetchone()[0]
        print("judge_cache: removed {} entries older than {} days (kept {}).".format(before - after, days, after))

        if args.prune_summary_history:
            protected = projection.protected_summary_ids(db)
            if protected:
                placeholders = ",".join(["?"] * len(protected))
                params = [cutoff] + sorted(protected)
                db.execute(
                    "DELETE FROM summary_versions WHERE created_at < ? "
                    f"AND id NOT IN ({placeholders})",
                    params,
                )
            else:
                db.execute(
                    "DELETE FROM summary_versions WHERE created_at < ?", (cutoff,)
                )
            db.commit()
            print(
                "summary_versions: pruned old unprotected versions older than {} days.".format(
                    days
                )
            )
    finally:
        db.close()
        lock.release()


def cmd_summary_audit(args):
    cwd = Path(args.path).resolve() if args.path else Path.cwd()
    if not cwd.is_dir():
        print("Error: directory not found: " + str(cwd), file=sys.stderr)
        sys.exit(1)
    db = _open_logic_db(cwd)
    try:
        rows = db.execute(
            "SELECT version, summary, status, decision_rationale, decision_dimension, "
            "decision_confidence, created_at "
            "FROM summary_versions WHERE node_kind = ? AND node_ref = ? "
            "ORDER BY version ASC",
            (args.node_kind, args.node_ref),
        ).fetchall()
        if not rows:
            print("No summary history for {}::{}.".format(args.node_kind, args.node_ref))
            return
        print("Summary history for {}::{} ({} versions)".format(args.node_kind, args.node_ref, len(rows)))
        for version, summary, status, rationale, dimension, confidence, created_at in rows:
            print("  v{} [{}] {}".format(version, status, created_at))
            if summary:
                try:
                    payload = json.loads(summary)
                    short = payload.get("short", "")
                    if short:
                        print("    short: {}".format(short))
                    if payload.get("full"):
                        print("    full: {}".format(payload["full"][:200] + ("..." if len(payload["full"]) > 200 else "")))
                except (json.JSONDecodeError, TypeError):
                    print("    summary: (unparseable)")
            if rationale:
                print("    rationale: {}".format(rationale))
            if dimension:
                print("    dimension: {} (confidence={})".format(dimension, confidence or "?"))
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="remy-cc", description="Remy - delegated CLI for configuration and summary maintenance")
    sub = parser.add_subparsers(dest="command")
    p_config = sub.add_parser("config", help="Open configuration UI (global by default, or --path for project)")
    p_config.add_argument("--path", default=None, help="Project root directory (opens project-level config)")

    p_rebuild = sub.add_parser("summary-rebuild", help="Rebuild file/cluster summaries (full bootstrap or targeted node)")
    p_rebuild.add_argument("--path", default=None, help="Project root directory (default: current directory)")
    p_rebuild.add_argument("--node-kind", choices=["file", "cluster"], default=None, help="Restrict to a single node kind")
    p_rebuild.add_argument("--node-ref", default=None, help="Restrict to a single node_ref (file path or cluster name)")
    p_rebuild.add_argument("--mode", choices=["auto", "ask", "never"], default=None, help="Override REMY_SUMMARY_BOOTSTRAP_MODE")

    p_vacuum = sub.add_parser("summary-vacuum", help="Delete judge_cache entries older than --older-than days")
    p_vacuum.add_argument("--path", default=None, help="Project root directory (default: current directory)")
    p_vacuum.add_argument("--older-than", type=int, default=90, help="Delete entries older than N days (default 90)")
    p_vacuum.add_argument("--prune-summary-history", action="store_true", help="Also prune non-latest summary_versions older than --older-than")

    p_audit = sub.add_parser("summary-audit", help="Show summary_versions history for a node_ref")
    p_audit.add_argument("node_kind", choices=["symbol", "file", "cluster"], help="Node kind to audit")
    p_audit.add_argument("node_ref", help="Node ref (file::name, file path, or cluster name)")
    p_audit.add_argument("--path", default=None, help="Project root directory (default: current directory)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "config": cmd_config,
        "summary-rebuild": cmd_summary_rebuild,
        "summary-vacuum": cmd_summary_vacuum,
        "summary-audit": cmd_summary_audit,
    }
    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
