#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Inject the configured short reminder using the hook event's working directory."""

import json
import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config


def load_reminder_text(cwd=None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root = remy_config.discover_project_root(cwd) if cwd is not None else None
    lang = str(remy_config.load_config(root or cwd, strict=False).get("REMY_LANG", "en"))
    suffix = "zh" if lang == "zh-CN" else "en"
    primary = os.path.join(script_dir, f"reminder_prompt_{suffix}.md")
    fallback = os.path.join(script_dir, f'reminder_prompt_{"en" if suffix == "zh" else "zh"}.md')
    for path in (primary, fallback):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                return stream.read().strip()
        except (OSError, UnicodeError):
            continue
    return "Reminder prompt files missing. Please reinstall the suite."


def main():
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    try:
        if sys.stdin.isatty():
            return
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook input must be an object")
        cwd = data.get("cwd")
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError("cwd must name an existing absolute directory")
        event = data.get("hook_event_name", "UserPromptSubmit")
        if event not in ("UserPromptSubmit", "SessionStart"):
            raise ValueError("unsupported reminder event")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": load_reminder_text(cwd),
        }}, ensure_ascii=False))
    except Exception as error:
        print(f"enforcer_hook: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
