#!/usr/bin/env python3
"""MCP server for remy-index: exposes code intelligence queries over stdio."""
import os
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
)

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
    return query_symbol_impl(name, file or None)


@mcp.tool()
def query_summary(name: str, file: str = "") -> str:
    """Get symbol summary and docstring. Use for quick understanding of a function's purpose."""
    return query_summary_impl(name, file or None)


@mcp.tool()
def query_callers(symbol: str, depth: int = 2, include_ambiguous: bool = False, static_only: bool = False) -> str:
    """Find upstream callers of a symbol via BFS. Returns callers grouped by depth level."""
    return query_callers_impl(symbol, depth, include_ambiguous, static_only)


@mcp.tool()
def query_callees(symbol: str, depth: int = 2, include_ambiguous: bool = False, static_only: bool = False) -> str:
    """Find downstream callees of a symbol via BFS. Returns callees grouped by depth level."""
    return query_callees_impl(symbol, depth, include_ambiguous, static_only)


@mcp.tool()
def query_impact(files: list[str], depth_up: int = 3, depth_down: int = 3, include_ambiguous: bool = False, static_only: bool = False) -> str:
    """Analyze impact radius for files. Shows upstream callers and downstream callees."""
    return query_impact_impl(files, depth_up, depth_down, include_ambiguous, static_only)


@mcp.tool()
def query_patterns(pattern_type: str = "", signal_name: str = "", file: str = "") -> str:
    """Query event/callback registration patterns (Django signals, PyQt signals, observer pattern)."""
    return query_patterns_impl(pattern_type or None, signal_name or None, file or None)


if __name__ == "__main__":
    mcp.run(transport="stdio")
