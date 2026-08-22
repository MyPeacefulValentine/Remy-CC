#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PermissionRequest hook that auto-approves Edit/Write/Read prompts and
Grep/Glob search prompts targeting three classes of model-managed artifacts:

1. Project-level .claude/ system artifacts (temp_* directories, history/,
   generated tree and index files). Settings files are never approved: paths
   whose first component under .claude/ starts with "settings" or
   "remy-config" produce no decision.
2. Per-project auto-memory files under ~/.claude/projects/<slug>/memory/.
   The rule matches on path shape (any <slug>), not on the current project's
   slug: the slug encoding is an undocumented Claude Code internal, and every
   file in a memory/ directory belongs to the same model-authored content
   class. Search tools may additionally target the memory/ directory itself:
   searching a directory whose files are all individually approved grants no
   new access.
3. Files inside the system temporary directory (tempfile.gettempdir()).
   Temp-dir probe scripts and scratch artifacts are model-managed content;
   the containment check bounds the rule to the OS temp root, so no path
   outside it is reachable through this rule.
4. Suite-deployed artifacts under ~/.claude/ (skills/, output-styles/,
   hooks/) for READ-ONLY tools only (Read/Grep/Glob). Edit/Write keeps
   prompting so deployed copies stay manifest-true. Files at the ~/.claude/
   root (settings, credentials, CLAUDE.md) are outside the three
   directories and never match.

On any miss, disabled gate, or internal error the hook exits 0 with empty
stdout, which Claude Code treats as "no decision" and falls back to the
normal permission prompt (fail-open). Exit code 2 would deny the permission
and is never used.

Normative reference: the Exemptions list in style.md §1.2 (Authorization Gate
owner). This hook is the mechanism-layer counterpart of that list; the three
target classes above are approved uniformly for Edit, Write, Read, Grep, and
Glob prompts, and settings-class paths always fall through to the normal
prompt. Grep/Glob carry the target in tool_input "path" (optional; absent
means no decision), and their directory targets follow the same containment
rules as file targets.
"""

import json
import os
import sys
import tempfile
import time

ALLOWED_TOOLS = ("Edit", "Write", "Read", "Grep", "Glob")
SEARCH_TOOLS = ("Grep", "Glob")
READ_ONLY_TOOLS = ("Read",) + SEARCH_TOOLS
SUITE_READONLY_DIRS = ("skills", "output-styles", "hooks")
ALLOWED_DIR_PREFIXES = ("temp_",)
ALLOWED_DIRS = ("history",)
ALLOWED_ROOT_FILE_PREFIXES = ("project_tree", "logic_tree", "logic_index", "tree_config")
DENIED_PREFIXES = ("settings", "remy-config")
MEMORY_DIR_NAME = "memory"

ALLOW_DECISION = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    }
}


def gate_enabled(cwd):
    """Read REMY_PERMISSION_GATE via remy_config; any failure disables the gate."""
    try:
        remy_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "remy-src"))
        if not os.path.isdir(remy_src):
            remy_src = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
        if remy_src not in sys.path:
            sys.path.insert(0, remy_src)
        import remy_config
        return bool(remy_config.load_config(cwd, strict=False).get("REMY_PERMISSION_GATE", True))
    except Exception:
        return False


def _contains(root, target):
    """True when target is root itself or lies under root."""
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        # Different drives on Windows.
        return False


def _decide_memory(target, allow_dir=False):
    """Allow files inside any per-project auto-memory directory.

    Matches ~/.claude/projects/<slug>/memory/<...>; returns "allow" on match
    and None otherwise. Shape-based on purpose: the <slug> encoding is an
    undocumented Claude Code internal, so the rule does not try to bind the
    slug to the current project. With allow_dir (search tools), the memory/
    directory itself also matches: every file it contains is individually
    approved, so searching it grants no new access.
    """
    projects_root = os.path.realpath(
        os.path.join(os.path.expanduser("~"), ".claude", "projects")
    )
    if not _contains(projects_root, target):
        return None
    rel = os.path.relpath(target, projects_root)
    parts = rel.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[1].lower() == MEMORY_DIR_NAME:
        return "allow"
    if allow_dir and len(parts) == 2 and parts[1].lower() == MEMORY_DIR_NAME:
        return "allow"
    return None


def _decide_temp(target):
    """Allow files inside the system temporary directory.

    Bounded by containment under tempfile.gettempdir(): traversal that
    resolves outside the OS temp root never matches.
    """
    temp_root = os.path.realpath(tempfile.gettempdir())
    if _contains(temp_root, target):
        return "allow"
    return None


def _decide_suite(target, read_only):
    """Allow read-only tools on suite-deployed artifacts under ~/.claude/
    (SUITE_READONLY_DIRS); ~/.claude/ root files never match."""
    if not read_only:
        return None
    claude_home = os.path.realpath(os.path.join(os.path.expanduser("~"), ".claude"))
    for sub in SUITE_READONLY_DIRS:
        if _contains(os.path.join(claude_home, sub), target):
            return "allow"
    return None


def decide(cwd, tool_name, file_path):
    """Classify one permission request.

    Returns "allow" or a "skip:<reason>" marker. Only "allow" emits a
    decision; every skip falls back to the normal permission prompt.
    """
    if tool_name not in ALLOWED_TOOLS:
        return "skip:tool"
    if not file_path or not cwd:
        return "skip:no_path"

    claude_dir = os.path.realpath(os.path.join(cwd, ".claude"))
    target = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
    target = os.path.realpath(target)

    if not _contains(claude_dir, target):
        if _decide_memory(target, allow_dir=tool_name in SEARCH_TOOLS) == "allow":
            return "allow"
        if _decide_temp(target) == "allow":
            return "allow"
        if _decide_suite(target, read_only=tool_name in READ_ONLY_TOOLS) == "allow":
            return "allow"
        return "skip:outside"

    rel = os.path.relpath(target, claude_dir)
    if rel == os.curdir:
        return "skip:root"
    head = rel.replace("\\", "/").split("/")[0].lower()

    # Deny check runs before any allow rule.
    for prefix in DENIED_PREFIXES:
        if head.startswith(prefix):
            return "skip:denied"

    for prefix in ALLOWED_DIR_PREFIXES:
        if head.startswith(prefix):
            return "allow"
    if head in ALLOWED_DIRS:
        return "allow"
    if "/" not in rel.replace("\\", "/"):
        for prefix in ALLOWED_ROOT_FILE_PREFIXES:
            if head.startswith(prefix):
                return "allow"
    return "skip:unlisted"


def _trace(cwd, message):
    try:
        log_dir = os.path.join(cwd, ".claude", "temp_log")
        os.makedirs(log_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(os.path.join(log_dir, "permission_gate_trace.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (stamp, message))
    except Exception:
        pass


def main():
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if stdin_reconfigure is not None:
        stdin_reconfigure(encoding="utf-8")
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    if stdout_reconfigure is not None:
        stdout_reconfigure(encoding="utf-8")

    trace = "--trace" in sys.argv[1:]

    try:
        if sys.stdin.isatty():
            sys.exit(0)

        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        cwd = input_data.get("cwd", os.getcwd())
        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")

        if not gate_enabled(cwd):
            if trace:
                _trace(cwd, "fired tool=%s path=%s outcome=skip:disabled" % (tool_name, file_path))
            sys.exit(0)

        outcome = decide(cwd, tool_name, file_path)
        if trace:
            _trace(cwd, "fired tool=%s path=%s outcome=%s" % (tool_name, file_path, outcome))

        if outcome == "allow":
            print(json.dumps(ALLOW_DECISION))
        sys.exit(0)

    except SystemExit:
        raise
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
