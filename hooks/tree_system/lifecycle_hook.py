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
SCOPE_UI_SCRIPT = os.path.join(
    os.path.expanduser("~"), ".claude",
    "remy-src", "logic_scope_ui.py"
)

LANGUAGE_DIRECTIVES = {
    "zh-CN": "Always respond in Chinese-simplified",
    "en": "Always respond in English",
}


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


def maybe_launch_scope_ui(cwd):
    if not os.path.exists(SCOPE_UI_SCRIPT):
        return

    inject_policy = os.environ.get("LOGIC_INDEX_AUTO_INJECT", "ALWAYS")
    if inject_policy != "ALWAYS":
        return

    cache_file = os.path.join(cwd, ".claude", "logic_index.json")
    if not os.path.exists(cache_file):
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
            resume_only = "--resume-only" in sys.argv

            if not resume_only:
                maybe_launch_scope_ui(cwd)

            update_tree(cwd)
            run_struct_scan(cwd)

            if not resume_only:
                generate_language_md()

                lang = os.environ.get("REMY_LANG", "en")
                version = _get_version()
                banner_file = "banner_zh.md" if lang == "zh-CN" else "banner_en.md"
                banner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), banner_file)
                try:
                    with open(banner_path, "r", encoding="utf-8") as f:
                        advice = "\n" + f.read().strip().format(version=version)
                except (OSError, KeyError, ValueError):
                    advice = "\n\U0001f42d Remy v" + version

                print(json.dumps({
                    "systemMessage": advice
                }))
            else:
                print(json.dumps({}))

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
