#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Session cwd anchor shared by the hook pipeline.

The anchor is the project root captured at SessionStart (source startup or
clear). Hooks that derive .claude/ artifact locations from the hook-input cwd
resolve against the anchor instead, so a mid-session cd (which Claude Code
reports as a changed cwd on every subsequent hook call) cannot relocate
project artifacts. Anchors are stored per session_id under the system temp
directory. Every function is fail-open: on any error the caller keeps the
cwd it already has.
"""

import os
import re
import tempfile
import time

ANCHOR_DIR_NAME = "remy-cc-anchors"
MAX_AGE_SECONDS = 14 * 24 * 3600
_SESSION_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _anchor_path(session_id):
    if not session_id:
        return None
    safe = _SESSION_ID_SAFE.sub("_", str(session_id))[:128]
    return os.path.join(tempfile.gettempdir(), ANCHOR_DIR_NAME, safe)


def _cleanup(anchor_dir):
    now = time.time()
    for name in os.listdir(anchor_dir):
        path = os.path.join(anchor_dir, name)
        try:
            if now - os.path.getmtime(path) > MAX_AGE_SECONDS:
                os.remove(path)
        except OSError:
            continue


def record(session_id, cwd):
    """Persist cwd as the session's anchor root. Silent on failure."""
    try:
        path = _anchor_path(session_id)
        if not path or not cwd:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(os.path.realpath(cwd))
        _cleanup(os.path.dirname(path))
    except Exception:
        pass


def read(session_id):
    """Return the recorded anchor root, or None."""
    try:
        path = _anchor_path(session_id)
        if not path:
            return None
        with open(path, "r", encoding="utf-8") as f:
            anchor = f.read().strip()
        return anchor or None
    except Exception:
        return None


def resolve_root(session_id, cwd):
    """Return the anchor root when one is recorded, else cwd unchanged."""
    anchor = read(session_id)
    return anchor if anchor else cwd


def drift(session_id, cwd):
    """Return the anchor root when cwd has drifted away from it, else None."""
    try:
        anchor = read(session_id)
        if not anchor or not cwd:
            return None
        if os.path.normcase(os.path.realpath(cwd)) != os.path.normcase(anchor):
            return anchor
        return None
    except Exception:
        return None
