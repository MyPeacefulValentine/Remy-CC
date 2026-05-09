#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PreToolUse hook that enriches Read/Glob/Grep results with call graph and layer context
from logic_index.json. Runs independently of pre_tool_guard.py.
"""

import sys
import json
import os

CACHE_FILE = os.path.join(".claude", "logic_index.json")


def _normalize_path(file_path, cwd):
    """Convert absolute or relative path to project-relative forward-slash form."""
    if os.path.isabs(file_path):
        try:
            rel = os.path.relpath(file_path, cwd)
        except ValueError:
            return None
        return rel.replace(os.sep, "/")
    return file_path.replace(os.sep, "/")


def _build_enrichment(target_path, cache):
    """Build enrichment text for a target file from the cache."""
    file_data = cache.get(target_path)
    if not file_data:
        return None

    parts = []

    layer = file_data.get("layer")
    if layer:
        parts.append(f"[Logic Context] {target_path} ({layer})")
    else:
        parts.append(f"[Logic Context] {target_path}")

    symbol_names = {s["name"] for s in file_data.get("symbols", [])}

    callees = []
    for call in file_data.get("calls", []):
        qualified = call.get("callee_qualified")
        if qualified:
            callees.append(qualified)
    if callees:
        unique = list(dict.fromkeys(callees))
        parts.append(f"  Calls into: {', '.join(unique[:15])}")

    callers = []
    for path, data in cache.items():
        if path == "_meta" or path == target_path:
            continue
        for call in data.get("calls", []):
            qualified = call.get("callee_qualified", "")
            if qualified.startswith(target_path + "::"):
                caller_qualified = f"{path}::{call['caller']}"
                callers.append(caller_qualified)
    if callers:
        unique = list(dict.fromkeys(callers))
        parts.append(f"  Called by: {', '.join(unique[:15])}")

    if len(parts) <= 1 and not callees and not callers:
        imports = file_data.get("imports", [])
        if imports:
            parts.append(f"  Imports: {', '.join(imports)}")
        else:
            return None

    return "\n".join(parts)


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

        if tool_name not in ("Read", "Glob", "Grep"):
            sys.exit(0)

        file_path = tool_input.get("file_path") or tool_input.get("path")
        if not file_path:
            sys.exit(0)

        cache_path = os.path.join(cwd, CACHE_FILE)
        if not os.path.exists(cache_path):
            sys.exit(0)

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except (json.JSONDecodeError, IOError):
            sys.exit(0)

        target = _normalize_path(file_path, cwd)
        if not target:
            sys.exit(0)

        enrichment = _build_enrichment(target, cache)
        if not enrichment:
            sys.exit(0)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": f"<logic_context>\n{enrichment}\n</logic_context>"
            }
        }
        print(json.dumps(response))
        sys.exit(0)

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
