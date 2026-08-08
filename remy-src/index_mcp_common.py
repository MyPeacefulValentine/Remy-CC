#!/usr/bin/env python3
"""Shared database access, config scope, and summary lookup for MCP queries."""
import os
import sqlite3
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Optional

_IMPACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "remy-index")
if _IMPACT_DIR not in sys.path:
    sys.path.insert(0, _IMPACT_DIR)
from retrieval_projection import select_current_summary
import remy_config

DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")
_DB_NOT_FOUND = "Error: logic_index.db not found. Run /remy-index to initialize the project index."


_DB_OVERRIDE: ContextVar[Optional[str]] = ContextVar("remy_index_db_override", default=None)
_QUERY_CONFIG: ContextVar[Optional[remy_config.ConfigSnapshot]] = ContextVar(
    "remy_index_query_config", default=None
)


def _query_scoped(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        existing = _QUERY_CONFIG.get()
        if existing is not None:
            return function(*args, **kwargs)
        snapshot = _config()
        token = _QUERY_CONFIG.set(snapshot)
        try:
            return function(*args, **kwargs)
        finally:
            _QUERY_CONFIG.reset(token)
    return wrapped


@contextmanager
def database_override(path):
    token = _DB_OVERRIDE.set(str(path))
    try:
        yield
    finally:
        _DB_OVERRIDE.reset(token)


def _config():
    active = _QUERY_CONFIG.get()
    if active is not None:
        return active
    snapshot = remy_config.load_config(strict=False)
    remy_config.emit_diagnostics(snapshot, prefix="MCPConfig")
    return snapshot


def _config_values():
    config = _config()
    return (
        config.get_int("REMY_MCP_BFS_MAX_DEPTH"),
        config.get_int("REMY_MCP_RESULT_LIMIT"),
        config.get_bool("REMY_MCP_STATIC_ONLY_DEFAULT"),
        config.get_int("REMY_FLOW_MAX_DEPTH"),
        config.get_int("REMY_FLOW_MAX_VISITED"),
    )


def _open_db(db_path=None):
    path = str(db_path or _DB_OVERRIDE.get() or _config().get("REMY_LOGIC_INDEX_DB_PATH"))
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=3000")
    return db


def get_latest_summary(db, node_kind, node_ref):
    current = select_current_summary(db, node_kind, node_ref)
    if current.get("id") is None:
        if current.get("status") is None:
            return None
        return {
            "short": None,
            "full": None,
            "status": current.get("status"),
        }
    return {
        "short": current.get("short"),
        "full": current.get("full"),
        "status": current.get("status"),
    }
