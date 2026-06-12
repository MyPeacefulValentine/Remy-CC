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

DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")
DIRTY_FILE = os.path.join(".claude", "logic_index_dirty")


def _consume_dirty_files(cwd, target_path):
    dirty_path = os.path.join(cwd, DIRTY_FILE)
    if not os.path.exists(dirty_path):
        return
    try:
        with open(dirty_path, 'r', encoding='utf-8') as f:
            dirty_paths = {line.strip() for line in f if line.strip()}
    except Exception:
        return
    if not dirty_paths:
        return

    db_rel = os.environ.get("LOGIC_INDEX_DB_PATH", DB_FILE_DEFAULT)
    db_path = os.path.join(cwd, db_rel)
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

        if os.path.exists(struct_scan_path):
            import subprocess
            args = [sys.executable, struct_scan_path, "--cwd", cwd, "--files"] + list(relevant)
            subprocess.run(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    except Exception:
        return

    remaining = dirty_paths - relevant
    try:
        if remaining:
            with open(dirty_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(remaining)) + '\n')
        else:
            os.remove(dirty_path)
    except Exception:
        pass


def _normalize_path(file_path, cwd):
    if os.path.isabs(file_path):
        try:
            rel = os.path.relpath(file_path, cwd)
        except ValueError:
            return None
        return rel.replace(os.sep, "/")
    return file_path.replace(os.sep, "/")


def _open_db(cwd):
    db_rel = os.environ.get("LOGIC_INDEX_DB_PATH", DB_FILE_DEFAULT)
    db_path = os.path.join(cwd, db_rel)
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

    tier_full_max = 200
    tier_mid_max = 1000
    cap = 15
    cap_large = 10
    sig_max = 80
    try:
        tier_full_max = int(os.environ.get("ENRICHMENT_TIER_FULL_MAX", 200))
    except (ValueError, TypeError):
        pass
    try:
        tier_mid_max = int(os.environ.get("ENRICHMENT_TIER_MID_MAX", 1000))
    except (ValueError, TypeError):
        pass
    try:
        cap = int(os.environ.get("ENRICHMENT_CAP", 15))
    except (ValueError, TypeError):
        pass
    try:
        cap_large = int(os.environ.get("ENRICHMENT_CAP_LARGE", 10))
    except (ValueError, TypeError):
        pass
    try:
        sig_max = int(os.environ.get("ENRICHMENT_SIG_MAX_CHARS", 80))
    except (ValueError, TypeError):
        pass

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
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

    try:
        if sys.stdin.isatty():
            sys.exit(0)

        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        cwd = input_data.get("cwd", os.getcwd())

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
