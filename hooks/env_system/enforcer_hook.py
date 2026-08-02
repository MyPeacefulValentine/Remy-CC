#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : env_enforcer.py
@Description : UserPromptSubmit hook that injects CRITICAL environment constraints.
               Reads configuration from hooks/config/reminder_prompt.md.
@Author      : MyPeacefulValentine
@CreationDate: 2026-01-10
"""

import sys
import os

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

def load_reminder_text():
    """
    Loads the reminder text from the external configuration file.
    Selects the file using the effective Remy language configuration.
    Returns a default string if the file cannot be found.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    lang = str(remy_config.load_config(strict=False).get("REMY_LANG", "en"))
    suffix = "zh" if lang == "zh-CN" else "en"
    primary = os.path.join(script_dir, f'reminder_prompt_{suffix}.md')
    fallback = os.path.join(script_dir, f'reminder_prompt_{"en" if suffix == "zh" else "zh"}.md')

    for path in (primary, fallback):
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read().strip()
        except Exception:
            continue

    return "Reminder prompt files missing. Please reinstall the suite."

def main():
    """
    Prints the reminder text to stdout, which will be injected into the context.
    """
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7 or safe fallback
        pass

    reminder_text = load_reminder_text()
    sys.stdout.buffer.write(reminder_text.encode('utf-8'))
    sys.exit(0)

if __name__ == "__main__":
    main()
