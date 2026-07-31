#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PostToolUse hook that records modified file paths to .claude/logic_index_dirty.
Triggered after Edit/Write operations to track files needing structural re-scan.
"""

import sys
import json
import os


def _load_index_state():
    skill_dir = os.path.join(os.path.expanduser("~"), ".claude", "skills", "remy-index")
    if not os.path.isdir(skill_dir):
        skill_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "remy-index")
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    from index_state import DirtyQueue
    return DirtyQueue


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
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        cwd = input_data.get("cwd", os.getcwd())

        if tool_name not in ("Edit", "Write"):
            sys.exit(0)

        file_path = tool_input.get("file_path")
        if not file_path:
            sys.exit(0)

        DirtyQueue = _load_index_state()
        DirtyQueue(cwd).record(file_path)

        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
