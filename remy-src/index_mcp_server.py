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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remy_config

if not remy_config.load_config(strict=False).get_bool("REMY_MCP_SERVER_ENABLED"):
    print("remy-index MCP server disabled (REMY_MCP_SERVER_ENABLED=false)", file=sys.stderr)
    sys.exit(0)

from index_mcp_facts import (
    query_symbol_impl,
    query_symbol_summary_impl,
    query_file_summary_impl,
    query_patterns_impl,
    query_cluster_summary_impl,
    query_cluster_files_impl,
)
from index_mcp_graph import (
    query_callers_impl,
    query_callees_impl,
    query_impact_impl,
    query_flow_impl,
)
from index_mcp_search import query_search_impl
from index_mcp_navigate import query_navigate_impl

_DB_REL_DEFAULT = os.path.join(".claude", "logic_index.db")
_freshness_warning = ""


def _resolve_git_head(root_dir, db=None):
    """Locate git HEAD that covers the indexed sources.

    Returns a ``(head, cwd)`` tuple where ``cwd`` is the directory in
    which the ``git rev-parse`` call succeeded; returns ``(None, None)``
    if no git context can be resolved. ``cwd`` is reusable for follow-up
    git invocations such as ``git status --porcelain``.

    Keep this implementation in sync with
    ``Remy-CC/skills/remy-index/struct_scan.py::_resolve_git_head``.
    """
    candidates = [root_dir]
    if db is not None:
        try:
            row = db.execute("SELECT path FROM files LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        if row:
            inferred = os.path.dirname(os.path.join(root_dir, row[0]))
            candidates.append(inferred)
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        try:
            head = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], text=True,
                stderr=subprocess.DEVNULL, cwd=candidate
            ).strip()
            return head, candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None, None


def _init_freshness():
    """Probe index staleness at startup (before the event loop).

    subprocess.run with pipe capture deadlocks inside asyncio's
    ProactorEventLoop on Windows, so all subprocess calls MUST happen
    here — never inside a tool handler.
    """
    global _freshness_warning

    db_path = str(remy_config.load_config(strict=False).get("REMY_LOGIC_INDEX_DB_PATH"))
    if not os.path.exists(db_path):
        return

    db = sqlite3.connect(db_path, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    try:
        stored_row = db.execute("SELECT value FROM meta WHERE key='source_commit'").fetchone()
        file_count_row = db.execute("SELECT value FROM meta WHERE key='file_count'").fetchone()
        total = int(file_count_row[0]) if file_count_row else 1

        try:
            head, git_cwd = _resolve_git_head(os.getcwd(), db)
            if not head:
                raise ValueError("git failed")

            if stored_row and stored_row[0] == head:
                status = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=5, cwd=git_cwd
                )
                dirty = [l for l in status.stdout.splitlines() if l.strip() and not l.startswith("??")]
                if not dirty:
                    return
                rate = len(dirty) / max(total, 1)
                if rate > 0.5:
                    _freshness_warning = f"[Warning: index may be stale — {len(dirty)} files modified since last scan. Consider running /remy-index.]"
                elif rate > 0.2:
                    _freshness_warning = f"[Warning: index may be stale — {len(dirty)} files modified since last scan. Consider running /remy-index.]"
                return
            elif stored_row:
                _freshness_warning = f"[Warning: index built at commit {stored_row[0][:8]}, current HEAD is {head[:8]}. Run /remy-index to rebuild.]"
                return
            else:
                raise ValueError("no stored commit")
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            pass

        all_files = db.execute("SELECT path, struct_hash FROM files").fetchall()
        if not all_files:
            return

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
            _freshness_warning = f"[Warning: index may be stale — {mismatches}/{sample_size} sampled files differ. Run /remy-index to rebuild.]"
        elif rate > 0.2:
            _freshness_warning = f"[Warning: index may be stale — {mismatches}/{sample_size} sampled files differ. Consider running /remy-index.]"
    finally:
        db.close()


def _with_freshness(result):
    if result.startswith("Error:"):
        return result
    if _freshness_warning:
        return f"{_freshness_warning}\n\n{result}"
    return result


mcp = FastMCP(
    "remy-index",
    instructions=(
        "Use this server to query code structure and call relationships in the indexed project.\n"
        "\n"
        "Prefer these tools over Read/Grep when your goal is to understand code:\n"
        "- To understand a function's purpose or signature: query_symbol_summary (instead of reading the source file)\n"
        "- To understand a file's overall role and key symbols: query_file_summary (instead of reading the whole file)\n"
        "- To find who calls a function or what it calls: query_callers / query_callees (instead of grep)\n"
        "- To assess which modules a file change would affect: query_impact (instead of manual search)\n"
        "- To locate where a symbol is defined: query_symbol (instead of glob/grep)\n"
        "- To search for a symbol when you don't know the exact name: query_search (fuzzy prefix/substring/typo)\n"
        "- To trace call paths between two or more symbols: query_flow (bidirectional BFS)\n"
        "- To get a subsystem-level overview: query_cluster_summary (cluster contracts, entry symbols)\n"
        "- To list a cluster's member files: query_cluster_files (optionally with short summaries)\n"
        "- To locate work by intent (\"where do I modify auth logic\"): query_navigate (LLM-ranked clusters/files)\n"
        "\n"
        "Index summaries are stored in English. Phrase query_search text and\n"
        "query_navigate intents in English for best lexical recall.\n"
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
def query_symbol_summary(name: str, file: str = "") -> str:
    """Get symbol-level summary and docstring. Use for quick understanding of a function/class purpose."""
    return _with_freshness(query_symbol_summary_impl(name, file or None))


@mcp.tool()
def query_file_summary(file: str) -> str:
    """Get file-level semantic summary: role, key symbols, layer, and status. Use for understanding a file's overall purpose before reading source."""
    return _with_freshness(query_file_summary_impl(file))


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
def query_search(text: str, limit: int = 10, file_hint: str = "",
                 match: str = "all", language: str = "",
                 symbol_type: str = "", path_hint: str = "") -> str:
    """Search symbols with all/any/phrase matching and structural filters. Summaries are indexed in English; English query text maximizes lexical recall."""
    return _with_freshness(query_search_impl(
        text,
        limit,
        file_hint or "",
        match=match,
        language=language,
        symbol_type=symbol_type,
        path_hint=path_hint,
    ))


@mcp.tool()
def query_flow(symbols: list[str], max_depth: int = 15, max_visited: int = 2000, static_only: bool = False) -> str:
    """Find call paths among named symbols via bidirectional BFS. Supports qualified syntax: bare name, file/path:name, or Class.method."""
    return _with_freshness(query_flow_impl(symbols, max_depth, max_visited, static_only))


@mcp.tool()
def query_cluster_summary(name: str = "") -> str:
    """Return subsystem-level summaries for one or all clusters: name, label, short/full descriptions, entry symbols, and file count."""
    return _with_freshness(query_cluster_summary_impl(name or None))


@mcp.tool()
def query_cluster_files(cluster: str, with_summary: bool = False) -> str:
    """List member files of a cluster (path + layer). Set with_summary=True to append short file summaries inline."""
    return _with_freshness(query_cluster_files_impl(cluster, with_summary))


@mcp.tool()
def query_navigate(intent: str, top_k: int = 5) -> str:
    """Locate work by natural-language intent over bounded cluster/file/symbol candidates. Returns top_k ranked entries with {cluster, file?, symbol?, relevance_score, rationale}. Index summaries are English; phrase the intent in English for lexical candidate recall (non-English intents fall back to cluster-level ranking)."""
    return _with_freshness(query_navigate_impl(intent, top_k))


if __name__ == "__main__":
    _init_freshness()
    mcp.run(transport="stdio")
