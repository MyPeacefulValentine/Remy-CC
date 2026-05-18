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

GENERATOR_SCRIPT = "generate_smart_tree.py"
STRUCT_SCAN_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".claude",
    "skills", "update-logic-index", "struct_scan.py"
)

LANGUAGE_DIRECTIVES = {
    "zh-CN": "Always respond in Chinese-simplified",
    "en": "Always respond in English",
}


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
    cache_file = os.path.join(cwd, ".claude", "logic_index.json")
    if not os.path.exists(cache_file):
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


def update_tree(cwd):
    """
    Executes the tree generation script.
    """
    # Resolve script path relative to this hook file, not CWD
    hook_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(hook_dir, GENERATOR_SCRIPT)

    if not os.path.exists(script_path):
        # Fail silently if script is missing to avoid blocking the session
        return

    try:
        subprocess.run(
            [sys.executable, script_path],
            cwd=cwd,
            check=True,
            stdout=subprocess.DEVNULL, # Silence stdout (avoid injecting context)
            stderr=subprocess.PIPE     # Capture stderr for logging if needed
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
            update_tree(cwd)
            run_struct_scan(cwd)
            generate_language_md()

            lang = os.environ.get("REMY_LANG", "en")
            if lang == "zh-CN":
                advice = (
                    "\n💡 提示：如果之前从未使用过 /update-tree 或刚安装 hooks，建议手动执行 /update-tree 以刷新项目结构上下文。\n"
                    "🛡️ 建议：请将 .claude/ 加入 .gitignore 以避免提交自动生成的元数据；执行/compact 前使用 /milestone 以固化历史记录。"
                )
            else:
                advice = (
                    "\n💡 Tip: If you have never used /update-tree or just installed hooks, run /update-tree manually to refresh the project structure context.\n"
                    "🛡️ Recommendation: Add .claude/ to .gitignore to avoid committing auto-generated metadata; use /milestone before /compact to persist history."
                )

            print(json.dumps({
                "systemMessage": advice
            }))

            sys.exit(0)

        if event_name == "PreCompact":
            update_tree(cwd)
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
