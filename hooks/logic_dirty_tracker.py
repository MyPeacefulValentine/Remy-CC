#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PostToolUse hook that records modified file paths to .claude/logic_index_dirty.
Triggered after Edit/Write operations to track files needing structural re-scan.
"""

import sys
import json
import os

DIRTY_FILE = os.path.join(".claude", "logic_index_dirty")


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

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

        if os.path.isabs(file_path):
            try:
                rel = os.path.relpath(file_path, cwd)
            except ValueError:
                sys.exit(0)
            rel = rel.replace(os.sep, "/")
        else:
            rel = file_path.replace(os.sep, "/")

        dirty_path = os.path.join(cwd, DIRTY_FILE)
        os.makedirs(os.path.dirname(dirty_path), exist_ok=True)
        with open(dirty_path, 'a', encoding='utf-8') as f:
            f.write(rel + '\n')

        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
