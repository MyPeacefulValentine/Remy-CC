#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remy_config
from install_runtime import InstallRuntime, InstallRuntimeError, OperationResult, roots_from_environment
from install_runtime.facade import result_for_error


def get_claude_home():
    return roots_from_environment().claude


def get_version():
    runtime = InstallRuntime(roots_from_environment())
    try:
        manifest = runtime.load_manifest()
    except InstallRuntimeError:
        return "unknown"
    return str(manifest.get("suite_version", "unknown")) if manifest else "unknown"


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


def cmd_ui(_args):
    _load_config_ui().main()


def cmd_project(args):
    project_dir = Path(args.path).resolve()
    if not project_dir.is_dir():
        print("Error: directory not found: " + str(project_dir), file=sys.stderr)
        sys.exit(1)
    _load_config_ui().main(mode="project", target_path=str(project_dir))


def cmd_config(args):
    if args.path:
        project_dir = Path(args.path).resolve()
        if not project_dir.is_dir():
            print("Error: directory not found: " + str(project_dir), file=sys.stderr)
            sys.exit(1)
        _load_config_ui().main(mode="project", target_path=str(project_dir))
    else:
        _load_config_ui().main()


def _emit_runtime_result(result, json_mode=False):
    if json_mode:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        for warning in result.warnings:
            print("  [X] " + warning)
        if result.exit_code == 0:
            print("Verification passed." if result.operation == "verify" else "Operation completed.")
    if result.exit_code:
        raise SystemExit(result.exit_code)


def cmd_verify_runtime(args):
    runtime = InstallRuntime(roots_from_environment())
    result = runtime.verify_environment()
    _emit_runtime_result(result, bool(getattr(args, "json", False)))


def cmd_uninstall_runtime(args):
    if not getattr(args, "yes", False) and not getattr(args, "non_interactive", False) and not getattr(args, "json", False):
        try:
            answer = input(_um("confirm")).strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print(_um("aborted"))
            return
    runtime = InstallRuntime(roots_from_environment())
    try:
        result = runtime.uninstall(purge_state=bool(getattr(args, "purge_state", False)))
    except InstallRuntimeError as exc:
        result = result_for_error("uninstall", exc)
    _emit_runtime_result(result, bool(getattr(args, "json", False)))


def cmd_version(_args):
    print("Remy v{}".format(get_version()))


def _remy_cc_home():
    try:
        return roots_from_environment().remy
    except InstallRuntimeError:
        print("Cannot determine home directory. Set $HOME and retry.", file=sys.stderr)
        raise SystemExit(1)


def cmd_daemon(args):
    exe_name = "remy-cc.exe" if sys.platform == "win32" else "remy-cc"
    exe = _remy_cc_home() / "bin" / exe_name
    if not exe.exists():
        print("Error: remy-cc binary not found at {}".format(exe), file=sys.stderr)
        print("Build it with: cargo build --release --manifest-path <repo>/remy-cc/Cargo.toml", file=sys.stderr)
        print("Then copy target/release/{} to {}".format(exe_name, exe.parent), file=sys.stderr)
        sys.exit(1)
    result = subprocess.run([str(exe)] + list(args.daemon_args))
    sys.exit(result.returncode)


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
    llm_call = _default_llm_call()
    db = _open_logic_db(cwd)
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
    db = _open_logic_db(cwd)
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


REPO_URL = "https://github.com/MyPeacefulValentine/Remy-CC.git"
BRANCH = "main"
VERSION_RAW_URL = "https://raw.githubusercontent.com/MyPeacefulValentine/Remy-CC/{}/VERSION".format(BRANCH)


def _fetch_remote_version():
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(VERSION_RAW_URL, headers={"User-Agent": "remy-cc"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip()
            if not raw or len(raw) > 20 or "<" in raw:
                return None
            return raw
    except (OSError, urllib.error.URLError):
        return None


def cmd_update(args):
    json_mode = bool(getattr(args, "json", False))

    def log(message):
        print(message, file=sys.stderr if json_mode else sys.stdout)

    if not shutil.which("git"):
        _emit_runtime_result(
            result_for_error("update", InstallRuntimeError("git is required for update")),
            json_mode,
        )

    local_ver = get_version()
    remote_ver = _fetch_remote_version()

    if remote_ver and local_ver == remote_ver:
        hook_mode = None
        try:
            hook_mode = InstallRuntime(roots_from_environment()).load_manifest().get("hook_mode")
        except InstallRuntimeError:
            pass
        _emit_runtime_result(
            OperationResult(
                operation="update",
                status="ok",
                exit_code=0,
                hook_mode=hook_mode,
                warnings=[],
            ),
            json_mode,
        )
        return

    if remote_ver:
        log("[*] Update available: v{} -> v{}".format(local_ver, remote_ver))
    else:
        log("[*] Could not determine remote version. Proceeding with update...")

    tmp_dir = tempfile.mkdtemp(prefix="remy-cc-update-")
    clone_dir = os.path.join(tmp_dir, "remy-cc")
    try:
        log("[*] Fetching latest version...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", BRANCH, REPO_URL, clone_dir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _emit_runtime_result(
                result_for_error("update", InstallRuntimeError("git clone failed")),
                json_mode,
            )

        log("[*] Running installer...")
        installer = os.path.join(clone_dir, "install.py")
        installer_args = [sys.executable, installer]
        if getattr(args, "non_interactive", False) or json_mode:
            installer_args.append("--non-interactive")
        if json_mode:
            installer_args.append("--json")
        rc = subprocess.run(installer_args).returncode
        if rc != 0:
            raise SystemExit(rc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Uninstall ─────────────────────────────────────────────────

_UNINSTALL_MSG = {
    "en": {
        "confirm": "This will remove all Remy-CC files and settings. Continue? [y/N] ",
        "aborted": "Uninstall cancelled.",
    },
    "zh-CN": {
        "confirm": "此操作将移除所有 Remy-CC 文件和配置。是否继续？[y/N] ",
        "aborted": "卸载已取消。",
    },
}


def _get_lang():
    return str(remy_config.load_config(strict=False).get("REMY_LANG", "en"))


def _um(key, **kwargs):
    lang = _get_lang()
    msgs = _UNINSTALL_MSG.get(lang, _UNINSTALL_MSG["en"])
    fallback = _UNINSTALL_MSG["en"].get(key) or key
    template = msgs.get(key) or fallback
    return template.format(**kwargs) if kwargs else template


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "ui":
            sys.argv[1:2] = ["config"]
        elif sys.argv[1] == "project" and len(sys.argv) > 2:
            project_path = sys.argv[2]
            sys.argv[1:3] = ["config", "--path", project_path]

    parser = argparse.ArgumentParser(prog="remy-cc", description="Remy - CLI for Claude Code configuration")
    sub = parser.add_subparsers(dest="command")
    p_config = sub.add_parser("config", help="Open configuration UI (global by default, or --path for project)")
    p_config.add_argument("--path", default=None, help="Project root directory (opens project-level config)")
    p_config.add_argument("--global", dest="global_flag", action="store_true", help="Explicitly open global config")

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

    p_daemon = sub.add_parser("daemon", help="Control the Remy-CC daemon")
    p_daemon.add_argument("daemon_args", nargs=argparse.REMAINDER, help="Arguments passed to remy-cc")

    p_update = sub.add_parser("update", help="Fetch and install latest version from remote")
    p_update.add_argument("--non-interactive", action="store_true", help="Disable installer prompts")
    p_update.add_argument("--json", action="store_true", help="Emit installer JSON result")
    p_uninstall = sub.add_parser("uninstall", help="Remove all Remy-CC files and settings")
    p_uninstall.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_uninstall.add_argument("--non-interactive", action="store_true", help="Disable prompts")
    p_uninstall.add_argument("--json", action="store_true", help="Emit one JSON result object")
    p_uninstall.add_argument("--purge-state", action="store_true", help="Also remove user-level engine state")
    p_verify = sub.add_parser("verify", help="Verify installation integrity")
    p_verify.add_argument("--json", action="store_true", help="Emit one JSON result object")
    sub.add_parser("version", help="Show installed version")
    args = parser.parse_args()

    commands = {
        "config": cmd_config,
        "summary-rebuild": cmd_summary_rebuild,
        "summary-vacuum": cmd_summary_vacuum,
        "summary-audit": cmd_summary_audit,
        "daemon": cmd_daemon,
        "update": cmd_update,
        "uninstall": cmd_uninstall_runtime,
        "verify": cmd_verify_runtime,
        "version": cmd_version,
    }
    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
