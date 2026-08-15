#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PreToolUse hook that enriches Read/Glob/Grep results with call graph and layer context
from logic_index.db. Runs independently of pre_tool_guard.py.
"""

import sys
import json
import os
import sqlite3
from collections import OrderedDict

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
try:
    import session_anchor
except Exception:
    session_anchor = None


def _load_index_state():
    skill_dir = os.path.join(os.path.expanduser("~"), ".claude", "skills", "remy-index")
    if not os.path.isdir(skill_dir):
        skill_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "remy-index")
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    from index_state import DirtyQueue, LockTimeoutError
    return DirtyQueue, LockTimeoutError


DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")


def _consume_dirty_files(cwd, target_path):
    DirtyQueue, LockTimeoutError = _load_index_state()
    queue = DirtyQueue(cwd)
    try:
        dirty_paths = queue.peek()
    except LockTimeoutError:
        return
    if not dirty_paths:
        return

    db_path = str(remy_config.load_config(cwd, strict=False).get("REMY_LOGIC_INDEX_DB_PATH"))
    if not os.path.exists(db_path):
        return

    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        imports_row = db.execute(
            "SELECT imports FROM files WHERE path = ?", (target_path,)
        ).fetchone()
        db.close()
    except Exception:
        return

    deps_of_target = set()
    if imports_row and imports_row[0]:
        try:
            deps_of_target = set(json.loads(imports_row[0]))
        except (json.JSONDecodeError, TypeError):
            pass

    relevant = dirty_paths & ({target_path} | deps_of_target)
    if not relevant:
        return

    try:
        claude_home = os.path.join(os.path.expanduser("~"), ".claude")
        struct_scan_path = os.path.join(claude_home, "skills", "remy-index", "struct_scan.py")

        if not os.path.exists(struct_scan_path):
            return
        import subprocess
        args = [
            sys.executable, struct_scan_path, "--cwd", cwd,
            "--lock-timeout", "0", "--consume-dirty", "--files", *sorted(relevant),
        ]
        subprocess.run(
            args, cwd=cwd, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=30,
        )
    except Exception:
        return


def _normalize_path(file_path, cwd):
    if os.path.isabs(file_path):
        try:
            rel = os.path.relpath(file_path, cwd)
        except ValueError:
            return None
        return rel.replace(os.sep, "/")
    return file_path.replace(os.sep, "/")


def _open_db(cwd):
    db_path = str(remy_config.load_config(cwd, strict=False).get("REMY_LOGIC_INDEX_DB_PATH"))
    if not os.path.exists(db_path):
        return None
    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        return db
    except Exception:
        return None


def _build_enrichment(target_path, db):
    file_row = db.execute(
        "SELECT layer, imports FROM files WHERE path = ?", (target_path,)
    ).fetchone()
    if not file_row:
        return None

    layer, imports_json = file_row
    file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    config = remy_config.load_config(strict=False)
    tier_full_max = config.get_int("REMY_ENRICHMENT_TIER_FULL_MAX")
    tier_mid_max = config.get_int("REMY_ENRICHMENT_TIER_MID_MAX")
    cap = config.get_int("REMY_ENRICHMENT_CAP")
    cap_large = config.get_int("REMY_ENRICHMENT_CAP_LARGE")
    sig_max = config.get_int("REMY_ENRICHMENT_SIG_MAX_CHARS")

    if tier_full_max > tier_mid_max:
        tier_full_max, tier_mid_max = 200, 1000
    if cap < cap_large:
        cap, cap_large = 15, 10
    if any(v < 0 for v in (tier_full_max, tier_mid_max, cap, cap_large, sig_max)):
        tier_full_max, tier_mid_max, cap, cap_large, sig_max = 200, 1000, 15, 10, 80

    if file_count > tier_mid_max:
        active_cap = cap_large
        detail_level = "minimal"
    elif file_count > tier_full_max:
        active_cap = cap
        detail_level = "mid"
    else:
        active_cap = cap
        detail_level = "full"

    parts = []
    if layer:
        parts.append(f"[Logic Context] {target_path} ({layer})")
    else:
        parts.append(f"[Logic Context] {target_path}")

    def _format_sig(args_str):
        if not args_str or sig_max <= 0:
            return ""
        if len(args_str) <= sig_max:
            return args_str
        arg_count = args_str.count(",") + 1
        return f"{args_str[:sig_max]}... ({arg_count} args)"

    def _get_sym_detail(qualified, detail):
        if "::" not in qualified:
            return None, qualified, None
        fpath, fname = qualified.split("::", 1)
        sym_row = db.execute(
            "SELECT lineno, end_lineno, args, layer FROM symbols s JOIN files f ON s.file_path = f.path WHERE s.file_path = ? AND s.name = ?",
            (fpath, fname)
        ).fetchone()
        if not sym_row:
            return fpath, fname, None
        lineno, end_lineno, args, f_layer = sym_row
        range_str = ""
        if lineno and end_lineno:
            range_str = f" [L{lineno}-L{end_lineno}]"
        elif lineno:
            range_str = f" [L{lineno}]"
        if detail == "full":
            sig = _format_sig(args) if args else ""
            entry_str = f"{fname}{range_str}"
            if sig:
                entry_str += f" | {sig}"
        elif detail == "mid":
            entry_str = f"{fname}{range_str}"
        else:
            entry_str = fname
        return fpath, entry_str, f_layer

    def _group_by_file(rows, detail):
        groups = OrderedDict()
        for (qualified,) in rows:
            fpath, entry_str, f_layer = _get_sym_detail(qualified, detail)
            if fpath is None:
                groups.setdefault("?", (None, []))[1].append(entry_str)
            else:
                if fpath not in groups:
                    groups[fpath] = (f_layer, [])
                groups[fpath][1].append(entry_str)
        parts = []
        for fpath, (f_layer, entries) in groups.items():
            layer_tag = f" ({f_layer})" if f_layer else ""
            if len(entries) == 1:
                parts.append(f"{fpath}{layer_tag}::{entries[0]}")
            else:
                parts.append(f"{fpath}{layer_tag}::{{{', '.join(entries)}}}")
        return parts

    callees = db.execute(
        "SELECT DISTINCT callee_qualified FROM edges WHERE source_file = ? AND callee_qualified IS NOT NULL LIMIT ?",
        (target_path, active_cap)
    ).fetchall()
    if callees:
        grouped = _group_by_file(callees, detail_level)
        parts.append(f"  Calls into: {', '.join(grouped)}")

    callers = db.execute(
        "SELECT DISTINCT source_file || '::' || caller FROM edges WHERE callee_qualified LIKE ? LIMIT ?",
        (target_path + '::%', active_cap)
    ).fetchall()
    if callers:
        grouped = _group_by_file(callers, detail_level)
        parts.append(f"  Called by: {', '.join(grouped)}")

    if len(parts) <= 1 and not callees and not callers:
        imports = []
        if imports_json:
            try:
                imports = json.loads(imports_json)
            except (json.JSONDecodeError, TypeError):
                pass
        if imports:
            parts.append(f"  Imports: {', '.join(imports)}")
        else:
            return None

    return "\n".join(parts)


def main():
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if stdin_reconfigure is not None:
        stdin_reconfigure(encoding="utf-8")
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if stdout_reconfigure is not None:
        stdout_reconfigure(encoding="utf-8")

    try:
        if sys.stdin.isatty():
            sys.exit(0)

        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        cwd = input_data.get("cwd", os.getcwd())
        if session_anchor is not None:
            cwd = session_anchor.resolve_root(input_data.get("session_id", ""), cwd)

        if tool_name not in ("Read", "Glob", "Grep"):
            sys.exit(0)

        file_path = tool_input.get("file_path") or tool_input.get("path")
        if not file_path:
            sys.exit(0)

        target = _normalize_path(file_path, cwd)
        if not target:
            sys.exit(0)

        _consume_dirty_files(cwd, target)

        db = _open_db(cwd)
        if not db:
            sys.exit(0)

        try:
            enrichment = _build_enrichment(target, db)
        finally:
            db.close()

        if not enrichment:
            sys.exit(0)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": f"<logic_context>\n{enrichment}\n</logic_context>"
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
