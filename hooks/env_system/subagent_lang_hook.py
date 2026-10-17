#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SubagentStart language context for the event working directory.
The hook supplies text, not an approval or a guarantee of language compliance.
Input or runtime errors produce a diagnostic and no decision.
"""

import json
import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

LANGUAGE_NAMES = {
    "zh-CN": "Chinese-simplified",
    "en": "English",
}


def build_directive(cwd):
    root = remy_config.discover_project_root(cwd)
    lang = str(remy_config.load_config(root or cwd, strict=False).get("REMY_LANG", "en"))
    language = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["en"])
    return "Respond in %s." % language


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
        if not isinstance(input_data, dict):
            raise ValueError("hook input must be an object")
        cwd = input_data.get("cwd")
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise ValueError("cwd must name an existing absolute directory")

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": build_directive(cwd),
            }
        }))
        sys.exit(0)

    except SystemExit:
        raise
    except Exception as error:
        print(f"subagent_lang_hook: {error}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
