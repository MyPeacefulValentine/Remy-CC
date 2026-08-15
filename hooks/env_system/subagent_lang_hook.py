#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SubagentStart hook that injects the configured response language into every
subagent's context via hookSpecificOutput.additionalContext. Replaces the
former style.md rule that required the model to append a language directive
to each Agent prompt by hand. Fail-open: on any error the hook exits 0 with
empty stdout and the subagent starts without the directive.
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
    lang = str(remy_config.load_config(cwd, strict=False).get("REMY_LANG", "en"))
    language = LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["en"])
    return "IMPORTANT: Output final response in %s." % language


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
        cwd = input_data.get("cwd", os.getcwd())

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": build_directive(cwd),
            }
        }))
        sys.exit(0)

    except SystemExit:
        raise
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
