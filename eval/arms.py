"""
Tool arms for the A/B comparison.

  A-baseline : grep / glob / read over the target source tree (pure Python,
               cross-platform — no dependency on a system `grep`).
  B-remy     : baseline + Remy-CC's MCP code-intelligence tools (additive).

Remy-CC tools are loaded from `index_mcp_server` (FastMCP) for their schemas and
dispatched through its `@mcp.tool` wrapper functions (which apply the tools'
default arguments and forward to `index_mcp_queries`), returning the SAME
formatted strings the live MCP server returns — so the token cost measured here
is faithful to real agent usage.

Tool definitions use the OpenAI function-calling shape:
  {"type": "function", "function": {"name", "description", "parameters"}}
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Callable

# Remy-CC repo root (this file lives at <repo>/eval/arms.py).
_REMY_ROOT = Path(__file__).resolve().parent.parent

_SKIP_DIRS = {".git", ".claude", "__pycache__", ".pytest_cache",
              "node_modules", ".vscode", ".idea", "dist", "build"}
_MAX_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MiB in grep


def _glob_match(pat: str, relpath: str, name: str) -> bool:
    """fnmatch accepting a filename glob ('*.py') or a path glob ('sub/*.py');
    '', '*', '**', '**/*' mean 'no filter'."""
    if not pat or pat in ("*", "**", "**/*"):
        return True
    if "/" in pat:
        return fnmatch.fnmatch(relpath, pat)
    return fnmatch.fnmatch(name, pat)


# ──────────────────────────────────────────────
# Baseline tools: grep / glob / read
# ──────────────────────────────────────────────

class BaselineTools:
    """
    grep/glob/read over a source tree, sandboxed under root.
    """

    def __init__(self, root: Path, grep_max_matches: int = 40,
                 read_max_lines: int = 200):
        self.root = Path(root).resolve()
        self.grep_max = grep_max_matches
        self.read_max = read_max_lines

    _SCHEMAS = {
        "grep": {
            "description": "Search file contents recursively with a regex (like `grep -rn`). "
                           "Returns up to N matches as `path:line: text`, relative to the repo root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "regex pattern"},
                    "glob": {"type": "string", "description": "optional file glob filter, e.g. '*.py'"},
                    "max_matches": {"type": "integer", "description": "cap on matches (default 40)"},
                },
                "required": ["pattern"],
            },
        },
        "glob": {
            "description": "Find files by glob pattern under the repo root. Returns relative paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob, e.g. '**/*.py' or 'skills/*.py'"},
                    "max": {"type": "integer", "description": "cap on results (default 50)"},
                },
                "required": ["pattern"],
            },
        },
        "read": {
            "description": "Read a range of lines from a file (relative to repo root). "
                           "Returns the lines with 1-based line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative file path"},
                    "start_line": {"type": "integer", "description": "1-based start (default 1)"},
                    "end_line": {"type": "integer", "description": "1-based end (inclusive)"},
                },
                "required": ["path"],
            },
        },
    }

    def definitions(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": name, **spec}}
                for name, spec in self._SCHEMAS.items()]

    def dispatch(self, name: str, args: dict) -> str:
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            return f"unknown tool: {name}"
        try:
            return fn(args)
        except Exception as e:  # surface errors to the agent
            return f"ERROR ({name}): {type(e).__name__}: {e}"

    def _resolve(self, rel: str) -> Path | None:
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            return None
        return p

    def _iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                yield Path(dirpath) / fn

    def _t_grep(self, args: dict) -> str:
        rx = re.compile(args["pattern"])
        glob_pat = args.get("glob") or ""
        max_m = int(args.get("max_matches") or self.grep_max)
        hits: list[str] = []
        for p in self._iter_files():
            rel = p.relative_to(self.root).as_posix()
            if not _glob_match(glob_pat, rel, p.name):
                continue
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()}")
                    if len(hits) > max_m:
                        break
            if len(hits) > max_m:
                break
        if len(hits) > max_m:
            return "\n".join(hits[:max_m]) + f"\n... (truncated at {max_m} matches)"
        return "\n".join(hits) if hits else "(no matches)"

    def _t_glob(self, args: dict) -> str:
        max_n = int(args.get("max") or 50)
        matches = sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.glob(args["pattern"])
            if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(self.root).parts)
        )
        if len(matches) > max_n:
            return "\n".join(matches[:max_n]) + f"\n... ({len(matches) - max_n} more)"
        return "\n".join(matches) if matches else "(no files)"

    def _t_read(self, args: dict) -> str:
        rel = args.get("path") or args.get("file")
        if not rel:
            return "read: missing 'path'"
        p = self._resolve(rel)
        if p is None or not p.is_file():
            return f"not found / outside repo: {rel}"
        s = int(args.get("start_line") or 1)
        lines = p.read_text(errors="replace").splitlines()
        e = args.get("end_line")
        e = int(e) if e else len(lines)
        e = min(e, s - 1 + self.read_max)
        chunk = lines[s - 1:e]
        return "\n".join(f"{i:>6}\t{ln}" for i, ln in enumerate(chunk, start=s))


# ──────────────────────────────────────────────
# Remy-CC MCP tools
# ──────────────────────────────────────────────

class RemyTools:
    """
    Loads Remy-CC's MCP tool schemas via FastMCP's public list_tools() API and
    dispatches to the module-level @mcp.tool functions (sync).
    """

    EXCLUDE = {"query_navigate"}

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self._mod = None
        self._schemas: dict[str, dict] = {}

    def load(self):
        import asyncio
        import sys
        for sub in ("remy-src", "skills/remy-index"):
            ap = str(_REMY_ROOT / sub)
            if ap not in sys.path:
                sys.path.insert(0, ap)
        import index_mcp_server as mod
        self._mod = mod
        for t in asyncio.run(mod.mcp.list_tools()):
            if t.name in self.EXCLUDE:
                continue
            self._schemas[t.name] = {
                "description": (t.description or "").strip(),
                "parameters": t.inputSchema,
            }

    def definitions(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": name, **spec}}
                for name, spec in self._schemas.items()]

    def dispatch(self, name: str, args: dict) -> str:
        if name not in self._schemas:
            return f"unknown tool: {name}"
        module = self._mod
        if module is None:
            return "Remy MCP tools are not loaded"
        fn = getattr(module, name, None)
        if not callable(fn):
            return f"tool not callable: {name}"
        try:
            with module.database_override(self.db_path):
                return str(fn(**args))
        except Exception as e:
            return f"ERROR ({name}): {type(e).__name__}: {e}"


# ──────────────────────────────────────────────
# Arm assembly
# ──────────────────────────────────────────────

def build_arm(arm: str, root: Path,
              db_path: Path | None) -> tuple[list[dict], Callable[[str, dict], str]]:
    """Return (tool_definitions, dispatch) for an arm.

    arm: "A-baseline" or "B-remy".
    """
    baseline = BaselineTools(root)
    if arm == "A-baseline":
        return baseline.definitions(), baseline.dispatch

    if arm == "B-remy":
        if not db_path:
            raise ValueError("B-remy requires a scoped logic_index.db path (--db)")
        remy = RemyTools(db_path)
        remy.load()
        defs = baseline.definitions() + remy.definitions()

        def dispatch(name: str, args: dict) -> str:
            if name in remy._schemas:
                return remy.dispatch(name, args)
            return baseline.dispatch(name, args)

        return defs, dispatch

    raise ValueError(f"unknown arm: {arm}")
