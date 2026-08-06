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

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

GENERATOR_SCRIPT = "generate_smart_tree.py"
STRUCT_SCAN_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".claude",
    "skills", "remy-index", "struct_scan.py"
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


def _generate_banner(version, lang):
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
    lines.append("─" * (box_width + 2))

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


def generate_language_md(cwd=None):
    lang = str(remy_config.load_config(cwd, strict=False).get("REMY_LANG", "en"))
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
        return None
    config = remy_config.load_config(cwd, strict=False)
    db_file = str(config.get("REMY_LOGIC_INDEX_DB_PATH"))
    json_file = os.path.join(cwd, ".claude", "logic_index.json")
    if not os.path.exists(db_file) and not os.path.exists(json_file):
        return None
    scan_timeout = config.get_int("REMY_STRUCT_SCAN_TIMEOUT")
    lock_timeout = config.get_float("REMY_INDEX_SCAN_LOCK_TIMEOUT")
    total_timeout = max(1.0, scan_timeout + lock_timeout + 5.0)
    try:
        completed = subprocess.run(
            [sys.executable, STRUCT_SCAN_SCRIPT, "--cwd", cwd,
             "--lock-timeout", str(lock_timeout), "--consume-dirty"],
            cwd=cwd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=total_timeout
        )
        if completed.returncode == 2:
            print(f"[StructScan] Partial: {completed.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
        elif completed.returncode != 0:
            print(f"[StructScan] Failed: {completed.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
        return completed.returncode
    except Exception as e:
        print(f"[StructScan] Unexpected error: {e}", file=sys.stderr)
        return 1


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
        event_name = input_data.get("hook_event_name", "") # For SessionStart/PreCompact

        if not event_name:
             event_name = input_data.get("hookName", "")

        cwd = input_data.get("cwd", os.getcwd())

        # Trigger update on specific lifecycle events
        if event_name == "SessionStart":
            resume_only = "--resume-only" in sys.argv

            config = remy_config.load_config(cwd, strict=False)

            update_tree(cwd, max_depth=2)
            run_struct_scan(cwd)

            if not resume_only:
                generate_language_md(cwd)

                lang = str(config.get("REMY_LANG", "en"))
                version = _get_version()

                if config.get_bool("REMY_BANNER_ENABLED"):
                    advice = _generate_banner(version, lang)
                    print(json.dumps({
                        "systemMessage": advice
                    }))
                else:
                    print(json.dumps({}))
            else:
                print(json.dumps({}))

            sys.exit(0)

        if event_name == "PreCompact":
            update_tree(cwd, max_depth=2)
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
