#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : tree_lifecycle.py
@Description : Automated project tree updater for SessionStart and PreCompact events.
               Ensures .claude/project_tree.md is fresh BEFORE the system prompts are assembled.
@Author      : MyPeacefulValentine
@CreationDate: 2026-01-26
"""

import sys
import json
import os
import subprocess
import unicodedata

GENERATOR_SCRIPT = "generate_smart_tree.py"
STRUCT_SCAN_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".claude",
    "skills", "remy-index", "struct_scan.py"
)
SCOPE_UI_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".claude",
    "remy-src", "logic_scope_ui.py"
)

LANGUAGE_DIRECTIVES = {
    "zh-CN": "Always respond in Chinese-simplified",
    "en": "Always respond in English",
}

BANNER_DATA_FILE = "banner_data.json"


def _display_width(s):
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return w


def _pad(s, target):
    return s + ' ' * max(0, target - _display_width(s))


def _generate_banner(version, lang, injection_mode=None):
    hook_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(hook_dir, BANNER_DATA_FILE)
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "\n\U0001f42d Remy v" + version

    GRAY = '\033[90m'
    RESET = '\033[39m'
    box_width = data.get("box_width", 74)
    col_widths = data.get("col_widths", [20, 30])

    header_text = data.get("header", {}).get(lang, "Welcome to Remy!")
    columns = data.get("columns", {}).get(lang, ["Skill", "Description", "When to Use"])
    footer = data.get("footer", {}).get(lang, "")
    skills = data.get("skills", [])

    desc_key = "desc_zh" if lang == "zh-CN" else "desc_en"
    timing_key = "timing_zh" if lang == "zh-CN" else "timing_en"

    lines = []
    lines.append("╭" + "─" * box_width + "╮")
    title_line = "  \U0001f42d " + header_text + " " + GRAY + "v" + version + RESET
    lines.append("│" + title_line + " " * (box_width - _display_width("  \U0001f42d " + header_text + " v" + version)) + "│")
    lines.append("╰" + "─" * box_width + "╯")
    lines.append("")
    lines.append("  " + _pad(columns[0], col_widths[0]) + _pad(columns[1], col_widths[1]) + columns[2])
    lines.append("  " + "─" * (col_widths[0] - 2) + "  " + "─" * (col_widths[1] - 2) + "  " + "─" * 22)

    for item in skills:
        if item.get("separator"):
            lines.append("")
            continue
        name = item.get("name", "")
        desc = item.get(desc_key, "")
        timing = item.get(timing_key, "")
        lines.append("  " + _pad(name, col_widths[0]) + _pad(desc, col_widths[1]) + timing)

    lines.append("")
    lines.append("  " + "─" * box_width)

    if injection_mode:
        im = data.get("injection_mode", {})
        lk = "zh" if lang == "zh-CN" else "en"
        lbl = im.get(f"label_{lk}", "📊 Injection")
        modes = im.get("modes", {})
        mode_info = modes.get(injection_mode, {})
        mode_str = mode_info.get(f"name_{lk}", injection_mode)
        color_code = mode_info.get("color", "0")
        hint = im.get(f"switch_hint_{lk}", "")
        COLOR = f'\033[{color_code}m'
        lines.append("")
        top_dashes = max(0, box_width - _display_width(lbl) - 5)
        lines.append("  ┌─ " + lbl + " " + "─" * top_dashes + "┐")
        mode_display = f"▶  {mode_str}"
        mode_content = f"▶  {COLOR}{mode_str}{RESET}"
        pad_mode = max(0, box_width - _display_width(mode_display) - 4)
        lines.append("  │  " + mode_content + " " * pad_mode + "│")
        hint_content = f"↳ {hint}"
        pad_hint = max(0, box_width - _display_width(hint_content) - 4)
        lines.append("  │  " + hint_content + " " * pad_hint + "│")
        lines.append("  └" + "─" * (box_width - 2) + "┘")
        lines.append("")

    cli_hint = data.get("cli_hint", {}).get(lang, "")
    if cli_hint:
        lines.append("  " + cli_hint)

    lines.append("  " + footer)

    return "\n" + "\n".join(lines)


def _get_version():
    manifest = os.path.join(os.path.expanduser("~"), ".claude", ".installer_manifest.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            return json.load(f).get("version", "dev")
    except (OSError, json.JSONDecodeError):
        return "dev"


def generate_language_md():
    lang = os.environ.get("REMY_LANG", "en")
    directive = LANGUAGE_DIRECTIVES.get(lang, LANGUAGE_DIRECTIVES["en"])
    claude_home = os.path.join(os.path.expanduser("~"), ".claude")
    lang_path = os.path.join(claude_home, "language.md")
    try:
        with open(lang_path, 'w', encoding='utf-8') as f:
            f.write(directive + "\n")
    except Exception as e:
        print(f"[LifecycleHook] Failed to generate language.md: {e}", file=sys.stderr)

def run_struct_scan(cwd):
    if not os.path.exists(STRUCT_SCAN_SCRIPT):
        return
    db_file = os.path.join(cwd, ".claude", "logic_index.db")
    json_file = os.path.join(cwd, ".claude", "logic_index.json")
    if not os.path.exists(db_file) and not os.path.exists(json_file):
        return
    try:
        scan_timeout = int(os.environ.get("STRUCT_SCAN_TIMEOUT", "60"))
    except ValueError:
        scan_timeout = 60
    try:
        subprocess.run(
            [sys.executable, STRUCT_SCAN_SCRIPT, "--cwd", cwd],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=scan_timeout
        )
    except subprocess.CalledProcessError as e:
        print(f"[StructScan] Failed: {e.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
    except Exception as e:
        print(f"[StructScan] Unexpected error: {e}", file=sys.stderr)


def maybe_launch_scope_ui(cwd, mcp_minimal=False):
    if mcp_minimal:
        return

    if not os.path.exists(SCOPE_UI_SCRIPT):
        return

    inject_policy = os.environ.get("LOGIC_INDEX_AUTO_INJECT", "ALWAYS")
    if inject_policy != "ALWAYS":
        return

    db_file = os.path.join(cwd, ".claude", "logic_index.db")
    json_file = os.path.join(cwd, ".claude", "logic_index.json")
    if not os.path.exists(db_file) and not os.path.exists(json_file):
        return

    interactive = os.environ.get("LOGIC_INDEX_INTERACTIVE", "true").lower()
    selection_file = os.path.join(cwd, ".claude", "logic_inject_selection.json")
    launch = False

    if interactive == "true":
        launch = True
    elif os.path.exists(selection_file):
        injector_path = os.path.join(os.path.expanduser("~"), ".claude", "hooks", "doc_manager", "injector.py")
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("injector", injector_path)
            _inj = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_inj)
            new_files = _inj.detect_new_logic_files(cwd)
            if new_files:
                launch = True
        except Exception:
            pass

    if not launch:
        return

    try:
        scope_timeout = int(os.environ.get("LOGIC_SCOPE_TIMEOUT", "300"))
        subprocess.run(
            [sys.executable, SCOPE_UI_SCRIPT, "--cwd", cwd, "--timeout", str(scope_timeout)],
            cwd=cwd,
            check=False,
            stdout=subprocess.DEVNULL,
            timeout=scope_timeout,
        )
    except subprocess.TimeoutExpired:
        print("[ScopeUI] Timed out, using existing selection", file=sys.stderr)
    except Exception as e:
        print(f"[ScopeUI] Error: {e}", file=sys.stderr)


def update_tree(cwd, max_depth=None):
    """
    Executes the tree generation script.
    """
    hook_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(hook_dir, GENERATOR_SCRIPT)

    if not os.path.exists(script_path):
        return

    cmd = [sys.executable, script_path]
    if max_depth is not None:
        cmd += ["--max-depth", str(max_depth)]

    try:
        subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        # If generation fails, we log to stderr but don't crash the hook
        print(f"[TreeUpdater] Failed to update tree: {e.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
    except Exception as e:
        print(f"[TreeUpdater] Unexpected error: {e}", file=sys.stderr)

def main():
    # Force UTF-8
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

    try:
        if sys.stdin.isatty():
            sys.exit(0)

        input_data = json.load(sys.stdin)
        event_name = input_data.get("hook_event_name", "") # For SessionStart/PreCompact

        if not event_name:
             event_name = input_data.get("hookName", "")

        cwd = input_data.get("cwd", os.getcwd())

        # Trigger update on specific lifecycle events
        if event_name == "SessionStart":
            resume_only = "--resume-only" in sys.argv

            import importlib.util
            claude_home = os.path.join(os.path.expanduser("~"), ".claude")
            server_script = os.path.join(claude_home, "remy-src", "index_mcp_server.py")
            mcp_available = os.path.exists(server_script) and importlib.util.find_spec("mcp") is not None
            mcp_minimal = (
                mcp_available
                and os.environ.get("NAV_MCP_MINIMAL_ENABLED", "true").lower() == "true"
            )

            if not resume_only:
                maybe_launch_scope_ui(cwd, mcp_minimal=mcp_minimal)

            update_tree(cwd, max_depth=2 if mcp_minimal else None)
            run_struct_scan(cwd)

            if not resume_only:
                generate_language_md()

                lang = os.environ.get("REMY_LANG", "en")
                version = _get_version()

                if os.environ.get("REMY_BANNER_ENABLED", "true").lower() != "false":
                    if mcp_minimal:
                        inj_mode = "mcp_minimal"
                    else:
                        db_path = os.path.join(cwd, ".claude", "logic_index.db")
                        if os.path.exists(db_path):
                            import sqlite3 as _sql
                            try:
                                _db = _sql.connect(db_path)
                                fc = _db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                                _db.close()
                                nav_full = int(os.environ.get("NAV_TIER_FULL_MAX", "200"))
                                nav_cluster = int(os.environ.get("NAV_TIER_CLUSTER_MAX", "2000"))
                                if fc <= nav_full:
                                    inj_mode = "full"
                                elif fc <= nav_cluster:
                                    inj_mode = "cluster"
                                else:
                                    inj_mode = "cluster_summary"
                            except Exception:
                                inj_mode = None
                        else:
                            inj_mode = None
                    advice = _generate_banner(version, lang, injection_mode=inj_mode)
                    print(json.dumps({
                        "systemMessage": advice
                    }))
                else:
                    print(json.dumps({}))
            else:
                print(json.dumps({}))

            sys.exit(0)

        if event_name == "PreCompact":
            import importlib.util
            _server = os.path.join(os.path.expanduser("~"), ".claude", "remy-src", "index_mcp_server.py")
            mcp_min = (
                os.path.exists(_server)
                and importlib.util.find_spec("mcp") is not None
                and os.environ.get("NAV_MCP_MINIMAL_ENABLED", "true").lower() == "true"
            )
            update_tree(cwd, max_depth=2 if mcp_min else None)
            run_struct_scan(cwd)
            print(json.dumps({}))
            sys.exit(0)

        if event_name == "SessionEnd":
            update_tree(cwd)
            print(json.dumps({}))
            sys.exit(0)

        # Fallback for unhandled events
        sys.exit(0)

    except Exception as e:
        # Fail safe
        print(f"[TreeUpdater] Critical Error: {e}", file=sys.stderr)
        sys.exit(0)

if __name__ == "__main__":
    main()
