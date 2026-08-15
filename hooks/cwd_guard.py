#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
CwdChanged hook that warns the user when the session cwd drifts away from
the anchored project root. CwdChanged has no model-visible output channel,
so this hook only emits a systemMessage terminal notification; the
model-visible reminder is injected by pre_tool_guard.py on the next tool
call, and .claude/ artifact writers resolve against the anchor regardless.
Fail-open: on any error the hook exits 0 with empty stdout.
"""

import json
import os
import sys

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
import session_anchor


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
        session_id = input_data.get("session_id", "")
        new_cwd = input_data.get("new_cwd", "") or input_data.get("cwd", "")

        anchor = session_anchor.drift(session_id, new_cwd)
        if anchor:
            print(json.dumps({
                "systemMessage": (
                    "⚠ Remy: cwd drifted from session root %s; "
                    ".claude artifact writes stay anchored there" % anchor
                )
            }, ensure_ascii=False))
        sys.exit(0)

    except SystemExit:
        raise
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
