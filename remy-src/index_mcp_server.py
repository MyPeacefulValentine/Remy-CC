#!/usr/bin/env python3
"""MCP server for remy-index: exposes code intelligence queries over stdio."""
import hashlib
import math
import os
import random
import sqlite3
import subprocess
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "Error: 'mcp' package not installed. Run: pip install mcp\n"
        "The remy-index MCP server requires Python 3.10+ and the mcp SDK.",
        file=sys.stderr,
    )
    sys.exit(0)

if os.environ.get("MCP_SERVER_ENABLED", "true").lower() == "false":
    print("remy-index MCP server disabled (MCP_SERVER_ENABLED=false)", file=sys.stderr)
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_mcp_queries import (
    query_symbol_impl,
    query_summary_impl,
    query_callers_impl,
    query_callees_impl,
    query_impact_impl,
    query_patterns_impl,
    query_search_impl,
    query_flow_impl,
)

_DB_REL_DEFAULT = os.path.join(".claude", "logic_index.db")
_freshness_cache = None


def _check_freshness():
    global _freshness_cache
    if _freshness_cache is not None:
        return _freshness_cache

    db_rel = os.environ.get("LOGIC_INDEX_DB_PATH", _DB_REL_DEFAULT)
    db_path = os.path.join(os.getcwd(), db_rel)
    if not os.path.exists(db_path):
        _freshness_cache = ""
        return ""

    db = sqlite3.connect(db_path, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    try:
        stored_row = db.execute("SELECT value FROM meta WHERE key='source_commit'").fetchone()
        file_count_row = db.execute("SELECT value FROM meta WHERE key='file_count'").fetchone()
        total = int(file_count_row[0]) if file_count_row else 1

        try:
            proc = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True, text=True, timeout=5
            )
            if proc.returncode != 0:
                raise ValueError("git failed")
            head = proc.stdout.strip()

            if stored_row and stored_row[0] == head:
                status = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=5
                )
                dirty = [l for l in status.stdout.splitlines() if l.strip() and not l.startswith("??")]
                if not dirty:
                    _freshness_cache = ""
                    return ""
                rate = len(dirty) / max(total, 1)
                if rate > 0.5:
                    _freshness_cache = f"[Warning: index may be stale — {len(dirty)} files modified since last scan. Consider running /remy-index.]"
                elif rate > 0.2:
                    _freshness_cache = f"[Warning: index may be stale — {len(dirty)} files modified since last scan. Consider running /remy-index.]"
                else:
                    _freshness_cache = ""
                return _freshness_cache
            elif stored_row:
                _freshness_cache = f"[Warning: index built at commit {stored_row[0][:8]}, current HEAD is {head[:8]}. Run /remy-index to rebuild.]"
                return _freshness_cache
            else:
                raise ValueError("no stored commit")
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            pass

        all_files = db.execute("SELECT path, struct_hash FROM files").fetchall()
        if not all_files:
            _freshness_cache = ""
            return ""

        sample_size = min(10, max(1, math.ceil(len(all_files) * 0.1)))
        sample = random.sample(all_files, sample_size)
        mismatches = 0
        for path, stored_hash in sample:
            if not os.path.exists(path):
                mismatches += 1
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                if hashlib.md5(content.encode('utf-8')).hexdigest() != stored_hash:
                    mismatches += 1
            except (OSError, UnicodeDecodeError):
                mismatches += 1

        rate = mismatches / sample_size
        if rate > 0.5:
            _freshness_cache = f"[Warning: index may be stale — {mismatches}/{sample_size} sampled files differ. Run /remy-index to rebuild.]"
        elif rate > 0.2:
            _freshness_cache = f"[Warning: index may be stale — {mismatches}/{sample_size} sampled files differ. Consider running /remy-index.]"
        else:
            _freshness_cache = ""
        return _freshness_cache
    finally:
        db.close()


def _with_freshness(result):
    if result.startswith("Error:"):
        return result
    warning = _check_freshness()
    if warning:
        return f"{warning}\n\n{result}"
    return result

mcp = FastMCP(
    "remy-index",
    instructions=(
        "Use this server to query code structure and call relationships in the indexed project.\n"
        "\n"
        "Prefer these tools over Read/Grep when your goal is to understand code:\n"
        "- To understand a function's purpose or signature: query_summary (instead of reading the source file)\n"
        "- To find who calls a function or what it calls: query_callers / query_callees (instead of grep)\n"
        "- To assess which modules a file change would affect: query_impact (instead of manual search)\n"
        "- To locate where a symbol is defined: query_symbol (instead of glob/grep)\n"
        "\n"
        "Do NOT use these tools when:\n"
        "- You need to read file content before making an edit (use Read instead)\n"
        "- You are reading configuration files, templates, or non-code assets\n"
        "- The target file is not part of the project's code index"
    ),
)


@mcp.tool()
def query_symbol(name: str, file: str = "") -> str:
    """Find symbol definitions by name. Returns location, type, signature, layer, and summary for each match."""
    return _with_freshness(query_symbol_impl(name, file or None))


@mcp.tool()
def query_summary(name: str, file: str = "") -> str:
    """Get symbol summary and docstring. Use for quick understanding of a function's purpose."""
    return _with_freshness(query_summary_impl(name, file or None))


@mcp.tool()
def query_callers(symbol: str, depth: int = 2, include_ambiguous: bool = False, static_only: bool = False) -> str:
    """Find upstream callers of a symbol via BFS. Returns callers grouped by depth level."""
    return _with_freshness(query_callers_impl(symbol, depth, include_ambiguous, static_only))


@mcp.tool()
def query_callees(symbol: str, depth: int = 2, include_ambiguous: bool = False, static_only: bool = False) -> str:
    """Find downstream callees of a symbol via BFS. Returns callees grouped by depth level."""
    return _with_freshness(query_callees_impl(symbol, depth, include_ambiguous, static_only))


@mcp.tool()
def query_impact(files: list[str], depth_up: int = 3, depth_down: int = 3, include_ambiguous: bool = False, static_only: bool = False) -> str:
    """Analyze impact radius for files. Shows upstream callers and downstream callees."""
    return _with_freshness(query_impact_impl(files, depth_up, depth_down, include_ambiguous, static_only))


@mcp.tool()
def query_patterns(pattern_type: str = "", signal_name: str = "", file: str = "") -> str:
    """Query event/callback registration patterns (Django signals, PyQt signals, observer pattern)."""
    return _with_freshness(query_patterns_impl(pattern_type or None, signal_name or None, file or None))


@mcp.tool()
def query_search(text: str, limit: int = 10, file_hint: str = "") -> str:
    """Fuzzy search symbols by name. Three-tier fallback: FTS5 prefix → LIKE substring → edit-distance. Use when you don't know the exact symbol name."""
    return _with_freshness(query_search_impl(text, limit, file_hint or ""))


@mcp.tool()
def query_flow(symbols: list[str], max_depth: int = 15, max_visited: int = 2000, static_only: bool = False) -> str:
    """Find call paths among named symbols via bidirectional BFS. Supports qualified syntax: bare name, file/path:name, or Class.method."""
    return _with_freshness(query_flow_impl(symbols, max_depth, max_visited, static_only))


if __name__ == "__main__":
    mcp.run(transport="stdio")
