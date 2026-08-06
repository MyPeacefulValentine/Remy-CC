#!/usr/bin/env python3
"""Query implementations for the remy-index MCP server."""
import difflib
import hashlib
import json
import os
import sys
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Optional

_IMPACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "remy-index")
sys.path.insert(0, _IMPACT_DIR)
from impact import (
    bfs_callers as _bfs_callers,
    bfs_callees as _bfs_callees,
    collect_file_symbols,
    get_file_count,
    get_layer,
    get_line_range,
)
from struct_scan import tokenize_symbol
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


def _bfs_callers_ambiguous(db, target_set, max_depth, static_only=False):
    visited = set(target_set)
    current = set(target_set)
    levels = {}
    prov_filter = "AND e.provenance IN ('definite','probable')" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ",".join(["?"] * len(chunk))
            sql = f"""
                SELECT DISTINCT source_file || '::' || caller FROM edges
                WHERE callee_qualified IN ({placeholders}) {prov_filter}
                UNION
                SELECT DISTINCT e.source_file || '::' || e.caller
                FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
                WHERE ec.candidate_qualified IN ({placeholders}) {prov_filter.replace('e.', 'e.')}
            """
            params = chunk + chunk
            all_rows.update(r[0] for r in db.execute(sql, params).fetchall())
        next_level = all_rows - visited
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        visited |= next_level
        current = next_level

    return levels


def _bfs_callees_ambiguous(db, target_set, max_depth, static_only=False):
    visited = set(target_set)
    current = set(target_set)
    levels = {}
    prov_filter = "AND e.provenance IN ('definite','probable')" if static_only else ""

    for depth in range(1, max_depth + 1):
        if not current:
            break
        current_list = list(current)
        all_rows = set()
        for i in range(0, len(current_list), 400):
            chunk = current_list[i:i+400]
            placeholders = ",".join(["?"] * len(chunk))
            sql = f"""
                SELECT DISTINCT callee_qualified FROM edges
                WHERE source_file || '::' || caller IN ({placeholders})
                AND callee_qualified IS NOT NULL {prov_filter}
                UNION
                SELECT DISTINCT ec.candidate_qualified
                FROM edges e JOIN edge_candidates ec ON ec.edge_id = e.id
                WHERE e.source_file || '::' || e.caller IN ({placeholders}) {prov_filter}
            """
            params = chunk + chunk
            all_rows.update(r[0] for r in db.execute(sql, params).fetchall())
        next_level = all_rows - visited
        if not next_level:
            break
        levels[depth] = sorted(next_level)
        visited |= next_level
        current = next_level

    return levels


def _resolve_symbol(db, name, file=None):
    if "::" in name:
        parts = name.split("::", 1)
        rows = db.execute(
            "SELECT file_path, name, type, args, lineno, end_lineno "
            "FROM symbols WHERE file_path = ? AND name = ?",
            (parts[0], parts[1]),
        ).fetchall()
    elif file:
        rows = db.execute(
            "SELECT file_path, name, type, args, lineno, end_lineno "
            "FROM symbols WHERE file_path = ? AND (name = ? OR short_name = ?)",
            (file, name, name),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT file_path, name, type, args, lineno, end_lineno "
            "FROM symbols WHERE name = ? OR short_name = ?",
            (name, name),
        ).fetchall()
    return rows[:_config_values()[1]]


@_query_scoped
def query_symbol_impl(name, file=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        rows = _resolve_symbol(db, name, file)
        if not rows:
            return f"No symbols found matching '{name}'"
        lines = [f"symbols matching '{name}' ({len(rows)} results)\n"]
        for fpath, sname, stype, args, lineno, end_lineno in rows:
            layer = get_layer(db, fpath)
            loc = f"L{lineno}" + (f"-L{end_lineno}" if end_lineno else "")
            sig = f"({args})" if args else ""
            lines.append(f"  [{stype}] {fpath}::{sname}{sig}  {fpath}:{loc} ({layer})")
            summary = get_latest_summary(db, "symbol", f"{fpath}::{sname}")
            if summary and summary.get("short"):
                lines.append(f"        {summary['short']}")
        return "\n".join(lines)
    finally:
        db.close()


@_query_scoped
def query_symbol_summary_impl(name, file=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        rows = _resolve_symbol(db, name, file)
        if not rows:
            return f"No symbols found matching '{name}'"
        lines = [f"summary for '{name}'\n"]
        for fpath, sname, stype, args, lineno, _end in rows:
            sig = f"({args})" if args else ""
            lines.append(f"  [{stype}] {fpath}::{sname}{sig}  L{lineno}")
            summary = get_latest_summary(db, "symbol", f"{fpath}::{sname}")
            if summary and summary.get("short"):
                lines.append(f"  summary: {summary['short']}")
                if summary.get("full"):
                    lines.append(f"  detail: {summary['full']}")
            else:
                lines.append("  summary: (no summary available)")
            lines.append("")
        return "\n".join(lines)
    finally:
        db.close()


@_query_scoped
def query_callers_impl(symbol, depth, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth = min(depth, _config_values()[0])
        if static_only is None:
            static_only = _config_values()[2]

        targets = set()
        if "::" in symbol:
            targets.add(symbol)
        else:
            rows = db.execute(
                "SELECT file_path || '::' || name FROM symbols WHERE name = ? OR short_name = ?",
                (symbol, symbol),
            ).fetchall()
            targets = {r[0] for r in rows}

        if not targets:
            return f"No symbols found matching '{symbol}'"

        if include_ambiguous:
            levels = _bfs_callers_ambiguous(db, targets, depth, static_only)
        else:
            levels = _bfs_callers(db, targets, depth, static_only)

        return _format_bfs_result(db, f"callers of {symbol}", levels, depth)
    finally:
        db.close()


@_query_scoped
def query_callees_impl(symbol, depth, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth = min(depth, _config_values()[0])
        if static_only is None:
            static_only = _config_values()[2]

        targets = set()
        if "::" in symbol:
            targets.add(symbol)
        else:
            rows = db.execute(
                "SELECT file_path || '::' || name FROM symbols WHERE name = ? OR short_name = ?",
                (symbol, symbol),
            ).fetchall()
            targets = {r[0] for r in rows}

        if not targets:
            return f"No symbols found matching '{symbol}'"

        if include_ambiguous:
            levels = _bfs_callees_ambiguous(db, targets, depth, static_only)
        else:
            levels = _bfs_callees(db, targets, depth, static_only)

        return _format_bfs_result(db, f"callees of {symbol}", levels, depth)
    finally:
        db.close()


@_query_scoped
def query_impact_impl(files, depth_up, depth_down, include_ambiguous, static_only):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        depth_up = min(depth_up, _config_values()[0])
        depth_down = min(depth_down, _config_values()[0])
        if static_only is None:
            static_only = _config_values()[2]

        target_files = []
        for f in files:
            normalized = f.replace("\\", "/")
            row = db.execute("SELECT path FROM files WHERE path = ?", (normalized,)).fetchone()
            if row:
                target_files.append(row[0])

        if not target_files:
            return f"No indexed files found matching: {', '.join(files)}"

        seeds = set()
        for tf in target_files:
            seeds |= collect_file_symbols(db, tf)

        if not seeds:
            return f"No symbols found in: {', '.join(target_files)}"

        if include_ambiguous:
            upstream = _bfs_callers_ambiguous(db, seeds, depth_up, static_only) if depth_up > 0 else {}
            downstream = _bfs_callees_ambiguous(db, seeds, depth_down, static_only) if depth_down > 0 else {}
        else:
            upstream = _bfs_callers(db, seeds, depth_up, static_only) if depth_up > 0 else {}
            downstream = _bfs_callees(db, seeds, depth_down, static_only) if depth_down > 0 else {}

        return _format_impact_result(db, target_files, seeds, upstream, downstream)
    finally:
        db.close()


@_query_scoped
def query_patterns_impl(pattern_type=None, signal_name=None, file=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        conditions = []
        params = []
        if pattern_type:
            conditions.append("pattern_type = ?")
            params.append(pattern_type)
        if signal_name:
            conditions.append("signal_name = ?")
            params.append(signal_name)
        if file:
            conditions.append("file_path = ?")
            params.append(file)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT file_path, pattern_type, signal_name, handler, line FROM patterns WHERE {where} LIMIT ?"
        params.append(_config_values()[1])

        rows = db.execute(sql, params).fetchall()
        if not rows:
            filters = []
            if pattern_type:
                filters.append(f"type={pattern_type}")
            if signal_name:
                filters.append(f"signal={signal_name}")
            if file:
                filters.append(f"file={file}")
            return f"No patterns found" + (f" ({', '.join(filters)})" if filters else "")

        lines = [f"event/callback patterns ({len(rows)} results)\n"]
        for fpath, ptype, signal, handler, line in rows:
            loc = f"L{line}" if line else ""
            lines.append(f"  [{ptype}] {signal or '?'} -> {handler or '?'}  {fpath}:{loc}")
        return "\n".join(lines)
    finally:
        db.close()


def _format_bfs_result(db, title, levels, max_depth):
    if not levels:
        return f"{title} ({max_depth} levels): no results"

    total = sum(len(v) for v in levels.values())
    lines = [f"{title} ({max_depth} levels, {total} results)\n"]

    for depth, qualified_list in sorted(levels.items()):
        lines.append(f"[depth {depth}]" + (" direct:" if depth == 1 else ""))
        count = 0
        for q in qualified_list:
            if count >= _config_values()[1]:
                lines.append(f"  ... ({len(qualified_list) - count} more)")
                break
            fpath = q.split("::")[0] if "::" in q else q
            layer = get_layer(db, fpath)
            lr = get_line_range(db, q)
            lines.append(f"  {q}{lr} ({layer})")
            count += 1
        lines.append("")

    return "\n".join(lines)


def _format_impact_result(db, target_files, seeds, upstream, downstream):
    all_files = set()
    lines = [f"impact analysis for: {', '.join(target_files)}\n"]

    lines.append("upstream (callers into these files):")
    if upstream:
        for depth, qualified_list in sorted(upstream.items()):
            entries = qualified_list[:_config_values()[1]]
            lines.append(f"  [depth {depth}] " + ", ".join(
                f"{q.split('::')[0]}" for q in entries[:5]
            ) + (f" ... +{len(entries)-5}" if len(entries) > 5 else ""))
            for q in entries:
                fpath = q.split("::")[0] if "::" in q else q
                all_files.add(fpath)
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("downstream (called by these files):")
    if downstream:
        for depth, qualified_list in sorted(downstream.items()):
            entries = qualified_list[:_config_values()[1]]
            lines.append(f"  [depth {depth}] " + ", ".join(
                f"{q.split('::')[0]}" for q in entries[:5]
            ) + (f" ... +{len(entries)-5}" if len(entries) > 5 else ""))
            for q in entries:
                fpath = q.split("::")[0] if "::" in q else q
                all_files.add(fpath)
    else:
        lines.append("  (none)")
    lines.append("")

    total_up = sum(len(v) for v in upstream.values())
    total_down = sum(len(v) for v in downstream.values())
    lines.append(f"summary: {len(all_files)} files affected, {total_up} upstream + {total_down} downstream symbols")

    return "\n".join(lines)


def _fts_available(db):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='retrieval_fts'"
    ).fetchone()
    return row is not None


_MATCH_MODES = frozenset(("all", "any", "phrase"))
_SYMBOL_TYPES = frozenset(
    ("function", "class", "struct", "enum", "typedef", "macro",
     "namespace", "interface", "type_alias")
)
_LANGUAGE_VALUES = {
    "python": ("pythonparser", "python"),
    "c_cpp": ("ccppparser", "c_cpp", "c", "cpp"),
    "typescript": ("tsparser", "typescript", "ts", "tsx"),
}
_FUZZY_CUTOFF = 0.6


@dataclass(frozen=True)
class _SearchQuery:
    text: str
    words: tuple[str, ...]
    match: str
    limit: int
    path_hint: str
    language: str
    language_values: tuple[str, ...]
    symbol_type: str

    @property
    def normalized_text(self):
        return " ".join(self.words)


class _SearchInputError(ValueError):
    pass


def _extract_search_words(text):
    words = []
    current = []
    for char in tokenize_symbol(text):
        category = unicodedata.category(char)
        if category[0] in ("L", "N") or (category[0] == "M" and current):
            current.append(char)
        elif current:
            words.append("".join(current).casefold())
            current = []
    if current:
        words.append("".join(current).casefold())
    return tuple(words)


def _normalize_path(value):
    return (value or "").strip().replace("\\", "/").casefold()


def _make_search_query(text, limit=10, file_hint="", *, match="all",
                       language="", symbol_type="", path_hint=""):
    if not isinstance(text, str):
        raise _SearchInputError("text must be a string")
    if not text.strip():
        raise _SearchInputError("text must not be empty")
    words = _extract_search_words(text)
    if not words:
        raise _SearchInputError("text must contain at least one searchable word")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _SearchInputError("limit must be an integer")
    if limit < 1 or limit > _config_values()[1]:
        raise _SearchInputError(f"limit must be between 1 and {_config_values()[1]}")
    if not isinstance(match, str):
        raise _SearchInputError("match must be a string")
    normalized_match = match.strip().casefold()
    if normalized_match not in _MATCH_MODES:
        raise _SearchInputError("match must be one of: all, any, phrase")

    for label, value in (("file_hint", file_hint), ("path_hint", path_hint)):
        if not isinstance(value, str):
            raise _SearchInputError(f"{label} must be a string")
        if "\x00" in value:
            raise _SearchInputError(f"{label} must not contain NUL")
    old_path = _normalize_path(file_hint)
    new_path = _normalize_path(path_hint)
    if old_path and new_path and old_path != new_path:
        raise _SearchInputError("file_hint and path_hint must not conflict")
    normalized_path = new_path or old_path

    if not isinstance(language, str):
        raise _SearchInputError("language must be a string")
    if language and not language.strip():
        raise _SearchInputError("language must not contain only whitespace")
    normalized_language = language.strip().casefold()
    if normalized_language and normalized_language not in _LANGUAGE_VALUES:
        raise _SearchInputError("language must be one of: python, c_cpp, typescript")

    if not isinstance(symbol_type, str):
        raise _SearchInputError("symbol_type must be a string")
    if symbol_type and not symbol_type.strip():
        raise _SearchInputError("symbol_type must not contain only whitespace")
    normalized_type = symbol_type.strip().casefold()
    if normalized_type and normalized_type not in _SYMBOL_TYPES:
        raise _SearchInputError(
            "symbol_type must be one of: " + ", ".join(sorted(_SYMBOL_TYPES))
        )

    return _SearchQuery(
        text=text.strip(),
        words=words,
        match=normalized_match,
        limit=limit,
        path_hint=normalized_path,
        language=normalized_language,
        language_values=_LANGUAGE_VALUES.get(normalized_language, ()),
        symbol_type=normalized_type,
    )


def _coerce_search_query(query_or_text, limit=None, file_hint=""):
    if isinstance(query_or_text, _SearchQuery):
        return query_or_text
    return _make_search_query(
        query_or_text, 10 if limit is None else limit, file_hint
    )


def _word_prefix_count(value, *terms):
    words = _extract_search_words(value or "")
    return sum(any(word.startswith(term) for word in words) for term in terms)


def _contains_phrase(value, phrase):
    words = _extract_search_words(value or "")
    terms = tuple((phrase or "").split())
    width = len(terms)
    if not width:
        return 0
    return int(any(words[index:index + width] == terms
                   for index in range(len(words) - width + 1)))


def _casefold_text(value):
    return (value or "").casefold()


def _register_search_functions(db):
    db.create_function("remy_norm_path", 1, _normalize_path, deterministic=True)
    db.create_function(
        "remy_word_prefix_count", -1, _word_prefix_count, deterministic=True
    )
    db.create_function("remy_contains_phrase", 2, _contains_phrase, deterministic=True)
    db.create_function("remy_casefold", 1, _casefold_text, deterministic=True)


def _append_search_filters(sql, params, query, *, projection_alias=None,
                           symbol_alias="s", file_alias="f"):
    if query.language_values:
        alias = projection_alias or file_alias
        placeholders = ",".join("?" for _ in query.language_values)
        sql += f"AND lower({alias}.language) IN ({placeholders}) "
        params.extend(query.language_values)
    if query.symbol_type:
        column = (f"{projection_alias}.symbol_type" if projection_alias
                  else f"{symbol_alias}.type")
        sql += f"AND lower({column}) = ? "
        params.append(query.symbol_type)
    if query.path_hint:
        alias = projection_alias or symbol_alias
        column = f"{alias}.file_path"
        sql += f"AND instr(remy_norm_path({column}), ?) > 0 "
        params.append(query.path_hint)
    return sql


def _fts_expression(query, terms=None):
    selected = query.words if terms is None else terms
    if query.match == "phrase":
        return '"' + " ".join(selected).replace('"', '""') + '"'
    separator = " OR " if query.match == "any" else " "
    return separator.join('"{}"*'.format(term.replace('"', '""'))
                          for term in selected)


def _fts_rows(db, query, expression, row_limit):
    sql = (
        "SELECT d.name, d.file_path, s.lineno, d.symbol_type, s.short_name, "
        "bm25(retrieval_fts, 0.0, 0.0, 0.0, 0.0, 5.0, 1.0) AS rank "
        "FROM retrieval_fts "
        "JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid "
        "JOIN symbols s ON s.file_path = d.file_path AND s.name = d.name "
        "WHERE retrieval_fts MATCH ? AND d.node_kind = 'symbol' "
    )
    params = ["{summary_short summary_full} : (" + expression + ")"]
    sql = _append_search_filters(sql, params, query, projection_alias="d")
    sql += "ORDER BY rank, lower(d.name), d.name, d.file_path, COALESCE(s.lineno, 0) LIMIT ?"
    params.append(row_limit)
    return db.execute(sql, params).fetchall()


def _search_fts(db, query_or_text, limit=None, file_hint="", diagnostics=None):
    query = _coerce_search_query(query_or_text, limit, file_hint)
    _register_search_functions(db)
    cap = query.limit * 5
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({"candidate_cap": cap, "per_term": []})

    if query.match == "any" and len(query.words) > 1:
        aggregated = {}
        for term in query.words:
            rows = _fts_rows(db, query, _fts_expression(query, (term,)), cap + 1)
            truncated = len(rows) > cap
            if diagnostics is not None:
                diagnostics["per_term"].append({
                    "term": term,
                    "candidate_count": min(len(rows), cap),
                    "truncated": truncated,
                })
            for name, fpath, lineno, stype, short, rank in rows[:cap]:
                key = (fpath, name)
                item = aggregated.setdefault(key, {
                    "name": name, "file_path": fpath, "lineno": lineno,
                    "symbol_type": stype, "short_name": short,
                    "coverage": 0, "rank": 0.0,
                })
                item["coverage"] += 1
                item["rank"] += rank
        items = list(aggregated.values())
        items.sort(key=lambda item: (
            -item["coverage"], item["rank"], item["name"].casefold(),
            item["name"], item["file_path"], item["lineno"] or 0,
        ))
        return [
            (item["name"], item["file_path"], item["lineno"],
             item["symbol_type"], item["rank"])
            for item in items[:query.limit]
        ]

    rows = _fts_rows(db, query, _fts_expression(query), cap + 1)
    if diagnostics is not None:
        diagnostics["truncated"] = len(rows) > cap
    results = []
    seen = set()
    for name, fpath, lineno, stype, _short, rank in rows[:cap]:
        key = (fpath, name)
        if key in seen:
            continue
        seen.add(key)
        results.append((name, fpath, lineno, stype, rank))
    results.sort(key=lambda row: (
        row[4], row[0].casefold(), row[0], row[1], row[2] or 0
    ))
    return results[:query.limit]


def _search_exact(db, query_or_text, limit=None, file_hint=""):
    query = _coerce_search_query(query_or_text, limit, file_hint)
    _register_search_functions(db)
    folded = query.text.casefold()
    sql = (
        "SELECT s.name, s.file_path, s.lineno, s.type FROM symbols s "
        "JOIN files f ON f.path = s.file_path "
        "WHERE (remy_casefold(s.name) = ? OR remy_casefold(s.short_name) = ?) "
    )
    params = [folded, folded]
    sql = _append_search_filters(sql, params, query)
    rows = db.execute(sql, params).fetchall()
    results = [(name, fpath, lineno, stype, 0.0)
               for name, fpath, lineno, stype in rows]
    results.sort(key=lambda row: (
        row[0].casefold(), row[0], row[1], row[2] or 0
    ))
    return results[:query.limit]


def _like_sort_key(row, query):
    name, fpath, lineno, _stype, _score = row
    name_folded = name.casefold()
    normalized_name = " ".join(_extract_search_words(name))
    normalized_query = query.normalized_text
    prefix_count = _word_prefix_count(normalized_name, *query.words)
    if name_folded == query.text.casefold():
        category = 0
    elif normalized_name == normalized_query:
        category = 1
    elif name_folded.startswith(query.text.casefold()):
        category = 2
    elif normalized_name.startswith(normalized_query):
        category = 3
    elif prefix_count:
        category = 4
    elif query.text.casefold() in name_folded:
        category = 5
    elif normalized_query in normalized_name:
        category = 6
    else:
        category = 7
    return (category, -prefix_count, name_folded, name, fpath, lineno or 0)


def _search_like(db, query_or_text, limit=None, file_hint=""):
    query = _coerce_search_query(query_or_text, limit, file_hint)
    _register_search_functions(db)
    sql = (
        "SELECT s.name, s.file_path, s.lineno, s.type FROM symbols s "
        "JOIN files f ON f.path = s.file_path WHERE "
    )
    params = []
    if query.match == "phrase":
        sql += "remy_contains_phrase(s.name_tokens, ?) = 1 "
        params.append(query.normalized_text)
    else:
        conditions = ["remy_word_prefix_count(s.name_tokens, ?) > 0"
                      for _ in query.words]
        joiner = " AND " if query.match == "all" else " OR "
        sql += "(" + joiner.join(conditions) + ") "
        params.extend(query.words)
    sql = _append_search_filters(sql, params, query)
    rows = db.execute(sql, params).fetchall()
    results = [(name, fpath, lineno, stype, 0.0)
               for name, fpath, lineno, stype in rows]
    results.sort(key=lambda row: _like_sort_key(row, query))
    return results[:query.limit]


def _search_fuzzy(db, query_or_text, limit=None, file_hint=""):
    query = _coerce_search_query(query_or_text, limit, file_hint)
    if any(char.isspace() for char in query.text):
        return []
    _register_search_functions(db)
    sql = (
        "SELECT s.name, s.file_path, s.lineno, s.type FROM symbols s "
        "JOIN files f ON f.path = s.file_path WHERE 1=1 "
    )
    params = []
    sql = _append_search_filters(sql, params, query)
    rows = db.execute(sql, params).fetchall()
    query_folded = query.text.casefold()
    results = []
    seen = set()
    for name, fpath, lineno, stype in rows:
        key = (fpath, name)
        if key in seen:
            continue
        seen.add(key)
        score = difflib.SequenceMatcher(
            None, query_folded, name.casefold()
        ).ratio()
        if score >= _FUZZY_CUTOFF:
            results.append((name, fpath, lineno, stype, score))
    results.sort(key=lambda row: (
        -row[4], row[0].casefold(), row[0], row[1], row[2] or 0
    ))
    return results[:query.limit]


def _channel_error(channel, error):
    return f"Error: {channel} search failed ({type(error).__name__})."


_CHANNEL_PRIORITY = {"exact": 0, "prefix": 1, "bm25": 2, "fuzzy": 3}


def _merge_candidates(channel_results, limit):
    merged = {}
    items = []
    for channel, rows in channel_results:
        priority = _CHANNEL_PRIORITY[channel]
        for rank, (name, fpath, lineno, stype, _score) in enumerate(rows, 1):
            key = (fpath, name)
            item = merged.get(key)
            if item is None:
                item = {
                    "name": name, "file_path": fpath, "lineno": lineno,
                    "symbol_type": stype, "sources": [],
                    "priority": priority, "best_rank": rank,
                }
                merged[key] = item
                items.append(item)
            item["sources"].append((channel, rank))
    items.sort(key=lambda item: (
        item["priority"], item["best_rank"], item["name"].casefold(),
        item["name"], item["file_path"], item["lineno"] or 0,
    ))
    return items[:limit]


def _result_detail(db, file_path, name):
    row = db.execute(
        "SELECT signature, summary_short FROM retrieval_documents "
        "WHERE node_kind = 'symbol' AND node_ref = ?",
        (f"{file_path}::{name}",),
    ).fetchone()
    if not row:
        return ""
    signature, summary = row
    parts = []
    if signature:
        parts.append(f"sig: ({signature})")
    if summary:
        parts.append(f"summary: {summary}")
    return " | ".join(parts)


@_query_scoped
def query_search_impl(text, limit=10, file_hint="", *, match="all",
                      language="", symbol_type="", path_hint=""):
    try:
        query = _make_search_query(
            text, limit, file_hint, match=match, language=language,
            symbol_type=symbol_type, path_hint=path_hint,
        )
    except _SearchInputError as error:
        return f"Error: {error}."

    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        try:
            if not _fts_available(db):
                return "Error: FTS index not available. Run struct_scan to rebuild the index."
        except sqlite3.Error as error:
            return _channel_error("FTS", error)

        deterministic = []
        for channel, label, search in (
            ("exact", "EXACT", _search_exact),
            ("prefix", "LIKE", _search_like),
            ("bm25", "FTS", _search_fts),
        ):
            try:
                deterministic.append((channel, search(db, query)))
            except sqlite3.Error as error:
                return _channel_error(label, error)
        results = _merge_candidates(deterministic, query.limit)
        search_level = "union"

        if not results:
            try:
                fuzzy_rows = _search_fuzzy(db, query)
            except sqlite3.Error as error:
                return _channel_error("fuzzy", error)
            results = _merge_candidates([("fuzzy", fuzzy_rows)], query.limit)
            search_level = "fuzzy"

        if not results:
            return f"No symbols found matching '{query.text}'"

        lines = [
            f"search results for '{query.text}' "
            f"({len(results)} results, matched via {search_level})\n"
        ]
        for item in results:
            fpath = item["file_path"]
            name = item["name"]
            lineno = item["lineno"]
            layer = get_layer(db, fpath)
            loc = f"L{lineno}" if lineno else ""
            lines.append(
                f"  [{item['symbol_type']}] {fpath}::{name}  {fpath}:{loc} ({layer})"
            )
            sources = ", ".join(
                f"{channel}#{rank}" for channel, rank in item["sources"]
            )
            lines.append(f"        sources: {sources} | priority={item['priority']}")
            detail = _result_detail(db, fpath, name)
            if detail:
                lines.append(f"        {detail}")
        return "\n".join(lines)
    finally:
        db.close()


def _load_graph(db, static_only=False):
    prov_filter = "AND provenance IN ('definite','probable')" if static_only else ""
    rows = db.execute(
        f"SELECT source_file, caller, callee_qualified, provenance, via "
        f"FROM edges WHERE callee_qualified IS NOT NULL {prov_filter}"
    ).fetchall()

    sym_rows = db.execute(
        "SELECT id, file_path || '::' || name, file_path, name, lineno, type "
        "FROM symbols"
    ).fetchall()

    name_to_id = {}
    id_to_info = {}
    for sid, qualified, fpath, sname, lineno, stype in sym_rows:
        name_to_id[qualified] = sid
        id_to_info[sid] = (qualified, fpath, sname, lineno, stype)

    adj_fwd = defaultdict(list)
    adj_bwd = defaultdict(list)
    skipped = 0
    for source_file, caller, callee_qualified, provenance, via in rows:
        src_q = f"{source_file}::{caller}"
        src_id = name_to_id.get(src_q)
        tgt_id = name_to_id.get(callee_qualified)
        if src_id is None or tgt_id is None:
            skipped += 1
            continue
        adj_fwd[src_id].append((tgt_id, provenance, via))
        adj_bwd[tgt_id].append((src_id, provenance, via))

    return adj_fwd, adj_bwd, name_to_id, id_to_info, skipped


def _bidir_bfs(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited):
    if src_id == tgt_id:
        return [(src_id, None, None)]

    fwd_parent = {src_id: (None, None, None)}
    bwd_parent = {tgt_id: (None, None, None)}
    front_f = [src_id]
    front_b = [tgt_id]

    for _depth in range(max_depth):
        if len(fwd_parent) + len(bwd_parent) > max_visited:
            return None
        if not front_f and not front_b:
            return None

        next_f = []
        for nid in front_f:
            for (t, prov, via) in adj_fwd.get(nid, []):
                if t not in fwd_parent:
                    fwd_parent[t] = (nid, prov, via)
                    next_f.append(t)
                if t in bwd_parent:
                    return _reconstruct_path(t, fwd_parent, bwd_parent, adj_fwd)
        front_f = next_f

        next_b = []
        for nid in front_b:
            for (s, prov, via) in adj_bwd.get(nid, []):
                if s not in bwd_parent:
                    bwd_parent[s] = (nid, prov, via)
                    next_b.append(s)
                if s in fwd_parent:
                    return _reconstruct_path(s, fwd_parent, bwd_parent, adj_fwd)
        front_b = next_b

    return None


def _reconstruct_path(meet, fwd_parent, bwd_parent, adj_fwd):
    fwd_half = []
    cur = meet
    while cur is not None:
        parent, prov, via = fwd_parent[cur]
        fwd_half.append((cur, prov, via))
        cur = parent
    fwd_half.reverse()

    bwd_half = []
    cur_bwd = bwd_parent[meet][0]
    while cur_bwd is not None:
        parent, _prov, _via = bwd_parent[cur_bwd]
        bwd_half.append(cur_bwd)
        cur_bwd = parent

    result = list(fwd_half)
    prev_id = meet
    for nid in bwd_half:
        edge_prov = None
        edge_via = None
        for (t, ep, ev) in adj_fwd.get(prev_id, []):
            if t == nid:
                edge_prov = ep
                edge_via = ev
                break
        result.append((nid, edge_prov, edge_via))
        prev_id = nid

    return result


def _resolve_flow_symbol(sym, db, name_to_id, adj_fwd, adj_bwd, resolved_ids, all_tokens):
    if "/" in sym and ":" in sym:
        idx = sym.rfind(":")
        file_part = sym[:idx]
        name_part = sym[idx + 1:]
        rows = db.execute(
            "SELECT file_path || '::' || name FROM symbols "
            "WHERE file_path LIKE ? AND (name = ? OR short_name = ?)",
            (f"%{file_part}%", name_part, name_part),
        ).fetchall()
        if rows:
            return name_to_id.get(rows[0][0]), rows[0][0], False
        return None, sym, False

    if "." in sym or "::" in sym:
        sep = "::" if "::" in sym else "."
        parts = sym.rsplit(sep, 1)
        class_hint = parts[0]
        method_name = parts[1]
        rows = db.execute(
            "SELECT file_path || '::' || name FROM symbols "
            "WHERE (name = ? OR short_name = ?)",
            (method_name, method_name),
        ).fetchall()
        candidates = [(q,) for (q,) in rows if class_hint.lower() in q.lower()]
        if candidates:
            return name_to_id.get(candidates[0][0]), candidates[0][0], False
        if rows:
            return name_to_id.get(rows[0][0]), rows[0][0], len(rows) > 1
        return None, sym, False

    rows = db.execute(
        "SELECT file_path || '::' || name FROM symbols "
        "WHERE name = ? OR short_name = ?",
        (sym, sym),
    ).fetchall()

    if not rows:
        return None, sym, False
    if len(rows) == 1:
        return name_to_id.get(rows[0][0]), rows[0][0], False

    candidates = [(q, name_to_id.get(q)) for (q,) in rows if name_to_id.get(q) is not None]
    if not candidates:
        return name_to_id.get(rows[0][0]), rows[0][0], True
    if len(candidates) == 1:
        return candidates[0][1], candidates[0][0], False

    if resolved_ids:
        connected = []
        for q, sid in candidates:
            reachable = set()
            frontier = [sid]
            min_depth = None
            for d in range(1, 3):
                nxt = []
                for n in frontier:
                    for (t, _, _) in adj_fwd.get(n, []):
                        if t not in reachable:
                            reachable.add(t)
                            nxt.append(t)
                    for (t, _, _) in adj_bwd.get(n, []):
                        if t not in reachable:
                            reachable.add(t)
                            nxt.append(t)
                frontier = nxt
                if min_depth is None and reachable & resolved_ids:
                    min_depth = d
            if min_depth is not None:
                deg = len(adj_fwd.get(sid, [])) + len(adj_bwd.get(sid, []))
                connected.append((min_depth, -deg, len(q), sid, q))
        if connected:
            connected.sort()
            chosen = connected[0]
            return chosen[3], chosen[4], False

    other_tokens = {t.lower() for t in all_tokens if t.lower() != sym.lower()}
    if other_tokens:
        for q, sid in candidates:
            q_lower = q.lower()
            if any(tok in q_lower for tok in other_tokens):
                return sid, q, False

    degree_list = []
    for q, sid in candidates:
        deg = len(adj_fwd.get(sid, [])) + len(adj_bwd.get(sid, []))
        degree_list.append((deg, len(q), sid, q))
    degree_list.sort(key=lambda x: (-x[0], x[1]))
    chosen = degree_list[0]
    return chosen[2], chosen[3], len(degree_list) > 1


def _format_flow(resolved, segments, id_to_info, static_only, max_depth):
    total_connected = sum(1 for s in segments if s is not None)
    if total_connected == 0:
        return "No connected paths found among the queried symbols."

    partial = total_connected < len(segments)
    if partial:
        header = f"## Flow (partial — {total_connected + 1}/{len(resolved)} symbols connected)\n"
    else:
        header = "## Flow (call path among queried symbols)\n"
    lines = [header]
    step = 1

    for i, (sym_id, sym_qualified, ambiguous) in enumerate(resolved):
        if sym_id is None:
            lines.append(f"\n[Unresolved: '{sym_qualified}' not found in index]\n")
            continue

        if i > 0 and segments[i - 1] is None:
            prev_name = resolved[i - 1][1].split("::")[-1] if "::" in resolved[i - 1][1] else resolved[i - 1][1]
            cur_name = sym_qualified.split("::")[-1] if "::" in sym_qualified else sym_qualified
            lines.append(f"\n[Break: pair ({prev_name}, {cur_name}) not connected within depth={max_depth}]")
            if static_only:
                lines.append("[Note: static_only=True excludes synthesized paths]")
            lines.append("")

        if i == 0 or segments[i - 1] is None:
            info = id_to_info.get(sym_id)
            if info:
                _, fpath, sname, lineno, _ = info
                loc = f":{lineno}" if lineno else ""
                amb_note = " [ambiguous: resolved by edge_count]" if ambiguous else ""
                lines.append(f"{step}. {sname} ({fpath}{loc}){amb_note}")
                step += 1

        if i < len(segments) and segments[i] is not None:
            path = segments[i]
            for j in range(1, len(path)):
                nid, prov, via = path[j]
                if prov == "inferred":
                    edge_label = f"synthesized [via: {via}]" if via else "synthesized"
                elif prov == "speculative":
                    edge_label = "call [speculative resolution]"
                elif prov == "probable":
                    edge_label = "call [name-match]"
                else:
                    edge_label = "call"
                lines.append(f"   ↓ {edge_label}")
                info = id_to_info.get(nid)
                if info:
                    _, fpath, sname, lineno, _ = info
                    loc = f":{lineno}" if lineno else ""
                    amb_note = ""
                    if j == len(path) - 1 and i + 1 < len(resolved) and resolved[i + 1][2]:
                        amb_note = " [ambiguous: resolved by edge_count]"
                    lines.append(f"{step}. {sname} ({fpath}{loc}){amb_note}")
                    step += 1

    return "\n".join(lines)


@_query_scoped
def query_flow_impl(symbols, max_depth=None, max_visited=None, static_only=False):
    if not symbols or len(symbols) < 2:
        return "Error: query_flow requires at least 2 symbols."

    config_values = _config_values()
    max_depth = min(config_values[3], config_values[3] if max_depth is None else max_depth)
    max_visited = min(config_values[4], config_values[4] if max_visited is None else max_visited)
    if static_only is None:
        static_only = config_values[2]

    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        adj_fwd, adj_bwd, name_to_id, id_to_info, _skipped = _load_graph(db, static_only)
        if not adj_fwd and not adj_bwd:
            return "No edges in index. Run struct_scan to build the call graph."

        all_tokens = []
        for sym in symbols:
            if "/" in sym and ":" in sym:
                all_tokens.append(sym.rsplit(":", 1)[-1])
            elif "." in sym:
                all_tokens.extend(sym.split("."))
            elif "::" in sym:
                all_tokens.extend(sym.split("::"))
            else:
                all_tokens.append(sym)

        resolved = []
        resolved_ids = set()
        for sym in symbols:
            sid, qualified, ambiguous = _resolve_flow_symbol(
                sym, db, name_to_id, adj_fwd, adj_bwd, resolved_ids, all_tokens
            )
            resolved.append((sid, qualified, ambiguous))
            if sid is not None:
                resolved_ids.add(sid)

        unresolved = [r[1] for r in resolved if r[0] is None]
        if len(unresolved) == len(resolved):
            return f"No symbols resolved: {', '.join(unresolved)}"

        segments = []
        for i in range(len(resolved) - 1):
            src_id = resolved[i][0]
            tgt_id = resolved[i + 1][0]
            if src_id is None or tgt_id is None:
                segments.append(None)
                continue
            path = _bidir_bfs(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited)
            segments.append(path)

        return _format_flow(resolved, segments, id_to_info, static_only, max_depth)
    finally:
        db.close()


@_query_scoped
def query_cluster_summary_impl(name=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        if name:
            rows = db.execute(
                "SELECT name, label, entry_symbols, file_count FROM clusters WHERE name = ?",
                (name,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT name, label, entry_symbols, file_count FROM clusters ORDER BY file_count DESC"
            ).fetchall()
        if not rows:
            return f"No clusters found" + (f" matching '{name}'" if name else "")
        lines = []
        for cluster_name, label, entry_json, file_count in rows:
            summary = get_latest_summary(db, "cluster", cluster_name)
            header = f"## {cluster_name} ({file_count} files)"
            if label and label != cluster_name:
                header += f"  [alias: {label}]"
            lines.append(header)
            if summary and summary.get("short"):
                lines.append(f"  short: {summary['short']}")
            if summary and summary.get("full"):
                lines.append(f"  full: {summary['full']}")
            try:
                entry_symbols = json.loads(entry_json) if entry_json else []
            except (json.JSONDecodeError, TypeError):
                entry_symbols = []
            if entry_symbols:
                lines.append(f"  entry_symbols: {', '.join(entry_symbols[:5])}")
            if summary and summary.get("status") and summary["status"] != "ok":
                lines.append(f"  status: {summary['status']}")
            lines.append("")
        return "\n".join(lines).rstrip()
    finally:
        db.close()


@_query_scoped
def query_file_summary_impl(file):
    if not file:
        return "Error: file path is required"
    file = file.replace("\\", "/")
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        row = db.execute("SELECT path FROM files WHERE path = ?", (file,)).fetchone()
        if not row:
            return f"No file '{file}' in index. Run /remy-index to scan."
        symbol_count = len(collect_file_symbols(db, file))
        layer = get_layer(db, file)
        summary = get_latest_summary(db, "file", file)
        lines = [f"## {file} ({symbol_count} symbols, layer={layer})"]
        if summary and summary.get("short"):
            lines.append(f"  short: {summary['short']}")
            if summary.get("full"):
                lines.append(f"  full: {summary['full']}")
        else:
            lines.append("  summary: (no summary available)")
        if summary and summary.get("status") and summary["status"] != "ok":
            lines.append(f"  status: {summary['status']}")
        return "\n".join(lines)
    finally:
        db.close()


@_query_scoped
def query_cluster_files_impl(cluster, with_summary=False):
    if not cluster:
        return "Error: cluster name is required"
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        row = db.execute(
            "SELECT id, label, file_count FROM clusters WHERE name = ?",
            (cluster,),
        ).fetchone()
        if not row:
            return (
                f"No cluster '{cluster}' found. "
                "Use query_cluster_summary() to list all clusters."
            )
        cluster_id, label, file_count = row
        member_rows = db.execute(
            "SELECT cm.file_path, f.layer FROM cluster_members cm "
            "JOIN files f ON cm.file_path = f.path "
            "WHERE cm.cluster_id = ? ORDER BY cm.file_path",
            (cluster_id,),
        ).fetchall()
        if not member_rows:
            return f"Cluster '{cluster}' has no member files."
        header = f"## {cluster} ({file_count} files)"
        if label and label != cluster:
            header += f"  [alias: {label}]"
        lines = [header]
        for fpath, layer in member_rows:
            layer_display = layer if layer else "Core"
            lines.append(f"  - {fpath}  (layer={layer_display})")
            if with_summary:
                summary = get_latest_summary(db, "file", fpath)
                if summary and summary.get("short"):
                    lines.append(f"      short: {summary['short']}")
                else:
                    lines.append("      short: (no summary available)")
        return "\n".join(lines)
    finally:
        db.close()


def _normalize_intent(intent):
    return " ".join(intent.lower().split())


_NAVIGATE_PROMPT_VERSION = "p1_4.1"
_NAVIGATE_DOC_COLUMNS = "{name name_tokens signature file_path summary_short summary_full}"
_NAVIGATE_DOC_WEIGHTS = "bm25(retrieval_fts, 1.0, 1.0, 0.0, 0.5, 5.0, 1.0)"
_NAVIGATE_KIND_ORDER = {"symbol": 0, "file": 1, "cluster": 2}


def _navigate_quotas():
    config = _config()
    return (
        config.get_int("REMY_NAVIGATE_CANDIDATE_CLUSTERS"),
        config.get_int("REMY_NAVIGATE_CANDIDATE_FILES"),
        config.get_int("REMY_NAVIGATE_CANDIDATE_SYMBOLS"),
    )


def _file_cluster_map(db):
    return dict(db.execute(
        "SELECT cm.file_path, c.name FROM cluster_members cm "
        "JOIN clusters c ON c.id = cm.cluster_id"
    ).fetchall())


def _navigate_doc_rows(db, query, kind, row_limit):
    sql = (
        "SELECT d.node_ref, d.summary_short, d.content_hash, "
        + _NAVIGATE_DOC_WEIGHTS + " AS rank "
        "FROM retrieval_fts "
        "JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid "
        "WHERE retrieval_fts MATCH ? AND d.node_kind = ? "
        "ORDER BY rank, d.node_ref LIMIT ?"
    )
    expression = _fts_expression(query)
    params = [_NAVIGATE_DOC_COLUMNS + " : (" + expression + ")", kind, row_limit]
    return db.execute(sql, params).fetchall()


def _navigate_symbol_rows(db, query):
    channels = []
    for channel, search in (
        ("exact", _search_exact),
        ("prefix", _search_like),
        ("bm25", _search_fts),
    ):
        channels.append((channel, search(db, query)))
    merged = _merge_candidates(channels, query.limit)
    if not merged and len(query.words) == 1:
        merged = _merge_candidates([("fuzzy", _search_fuzzy(db, query))], query.limit)
    return merged


def _navigate_symbol_docs(db, refs):
    docs = {}
    for ref in refs:
        row = db.execute(
            "SELECT content_hash, summary_short FROM retrieval_documents "
            "WHERE node_kind = 'symbol' AND node_ref = ?",
            (ref,),
        ).fetchone()
        docs[ref] = (row[0], row[1]) if row else ("", None)
    return docs


def _navigate_candidates(db, intent):
    """Bounded cluster/file/symbol candidates for one intent (lexical stage)."""
    try:
        k_cluster, k_file, k_symbol = _navigate_quotas()
        symbol_query = _make_search_query(
            intent, min(k_symbol, _config_values()[1]), match="any"
        )
    except _SearchInputError:
        return []
    _register_search_functions(db)
    file_to_cluster = _file_cluster_map(db)
    candidates = []

    for kind, quota in (("cluster", k_cluster), ("file", k_file)):
        rows = _navigate_doc_rows(db, symbol_query, kind, quota)
        for position, (node_ref, short, chash, _rank) in enumerate(rows, 1):
            candidates.append({
                "kind": kind,
                "node_ref": node_ref,
                "cluster": node_ref if kind == "cluster"
                           else file_to_cluster.get(node_ref, "(unclustered)"),
                "file": None if kind == "cluster" else node_ref,
                "symbol": None,
                "short": short or "",
                "content_hash": chash or "",
                "sources": [("bm25", position)],
            })

    merged = _navigate_symbol_rows(db, symbol_query)[:k_symbol]
    refs = [f"{item['file_path']}::{item['name']}" for item in merged]
    docs = _navigate_symbol_docs(db, refs)
    for item, ref in zip(merged, refs):
        chash, short = docs[ref]
        candidates.append({
            "kind": "symbol",
            "node_ref": ref,
            "cluster": file_to_cluster.get(item["file_path"], "(unclustered)"),
            "file": item["file_path"],
            "symbol": item["name"],
            "short": short or "",
            "content_hash": chash or "",
            "sources": list(item["sources"]),
        })
    return candidates


def _cluster_fallback_candidates(db, clusters):
    candidates = []
    for cluster in clusters:
        row = db.execute(
            "SELECT content_hash FROM retrieval_documents "
            "WHERE node_kind = 'cluster' AND node_ref = ?",
            (cluster["name"],),
        ).fetchone()
        candidates.append({
            "kind": "cluster",
            "node_ref": cluster["name"],
            "cluster": cluster["name"],
            "file": None,
            "symbol": None,
            "short": cluster["short"] or cluster["label"] or "",
            "content_hash": row[0] if row else "",
            "sources": [],
        })
    return candidates


def _navigate_cache_key(intent, top_k, candidates):
    payload = {
        "intent": _normalize_intent(intent),
        "top_k": top_k,
        "template": _NAVIGATE_PROMPT_VERSION,
        "candidates": [
            [entry["kind"], entry["node_ref"], entry["content_hash"]]
            for entry in candidates
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "navigate:" + hashlib.sha256(encoded).hexdigest()


def _collect_cluster_corpus(db):
    cluster_rows = db.execute(
        "SELECT name, label FROM clusters ORDER BY file_count DESC"
    ).fetchall()
    clusters = []
    for cname, label in cluster_rows:
        summary = get_latest_summary(db, "cluster", cname)
        clusters.append({
            "name": cname,
            "label": label,
            "short": summary.get("short") if summary else None,
        })
    return clusters


def _collect_navigate_corpus(db):
    clusters = _collect_cluster_corpus(db)
    file_rows = db.execute("SELECT path FROM files").fetchall()
    files = []
    for (fpath,) in file_rows:
        summary = get_latest_summary(db, "file", fpath)
        files.append({
            "path": fpath,
            "short": summary.get("short") if summary else None,
        })
    return clusters, files


@_query_scoped
def query_navigate_impl(intent, top_k=5, llm_call=None):
    db = _open_db()
    if not db:
        return _DB_NOT_FOUND
    try:
        if not intent or not intent.strip():
            return "Error: intent must not be empty."
        top_k = max(1, min(top_k, 20))

        candidates = _navigate_candidates(db, intent)

        if llm_call is None:
            llm_call = _try_default_llm_call()

        source = "llm"
        if not candidates:
            clusters = _collect_cluster_corpus(db)
            has_files = db.execute("SELECT 1 FROM files LIMIT 1").fetchone()
            if not clusters and not has_files:
                return "No clusters or files indexed; run /remy-index first."
            if llm_call is None or not clusters:
                return f"No matches for intent '{intent}' (source=heuristic)."
            candidates = _cluster_fallback_candidates(db, clusters)
            source = "llm-cluster-only"

        cache_key = _navigate_cache_key(intent, top_k, candidates)
        row = db.execute(
            "SELECT result FROM judge_cache WHERE payload_hash = ?", (cache_key,)
        ).fetchone()
        if row:
            try:
                cached = json.loads(row[0])
                if isinstance(cached, list):
                    return _format_navigate(cached, intent, source="cache")
            except json.JSONDecodeError:
                pass

        if llm_call is None:
            ranked = _heuristic_navigate(candidates, top_k)
            return _format_navigate(ranked, intent, source="heuristic")

        prompt = _build_navigate_prompt(intent, candidates, top_k)
        raw = llm_call(prompt)
        ranked = _parse_navigate_response(raw, top_k)
        if not ranked:
            ranked = _heuristic_navigate(candidates, top_k)
            return _format_navigate(ranked, intent, source="heuristic-fallback")

        from datetime import datetime as _dt
        db.execute(
            "INSERT OR REPLACE INTO judge_cache (payload_hash, result, created_at) VALUES (?,?,?)",
            (cache_key, json.dumps(ranked, ensure_ascii=False),
             _dt.now().isoformat(timespec="seconds")),
        )
        db.commit()
        return _format_navigate(ranked, intent, source=source)
    finally:
        db.close()


def _try_default_llm_call():
    if not _config().get("REMY_LLM_API_KEY"):
        return None
    try:
        sys.path.insert(0, _IMPACT_DIR)
        from run import LogicIndexer
    except ImportError:
        return None

    def _call(prompt):
        indexer = LogicIndexer(os.getcwd())
        return indexer._call_llm(prompt)

    return _call


def _build_navigate_prompt(intent, candidates, top_k):
    payload = {
        "intent": intent,
        "top_k": top_k,
        "candidates": [
            {
                "kind": entry["kind"],
                "cluster": entry["cluster"],
                "file": entry["file"],
                "symbol": entry["symbol"],
                "short": entry["short"],
            }
            for entry in candidates
        ],
    }
    return (
        "Task: Rank the candidate code locations by relevance to the given intent. "
        "Choose only from the provided candidates. "
        "Return a JSON array of <= top_k entries, each "
        "{\"cluster\": str, \"file\": str|null, \"symbol\": str|null, "
        "\"relevance_score\": float in [0,1], \"rationale\": str}.\n"
        "Higher scores indicate stronger match. Output JSON only, no prose.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _parse_navigate_response(raw, top_k):
    if not isinstance(raw, str) or raw.startswith("Error:"):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    cleaned = []
    for entry in data[:top_k]:
        if not isinstance(entry, dict):
            continue
        cluster = entry.get("cluster")
        if not isinstance(cluster, str):
            continue
        try:
            score = float(entry.get("relevance_score", 0))
        except (TypeError, ValueError):
            score = 0.0
        cleaned.append({
            "cluster": cluster,
            "file": entry.get("file") if isinstance(entry.get("file"), str) else None,
            "symbol": entry.get("symbol") if isinstance(entry.get("symbol"), str) else None,
            "relevance_score": max(0.0, min(1.0, score)),
            "rationale": entry.get("rationale", "") if isinstance(entry.get("rationale"), str) else "",
        })
    cleaned.sort(key=lambda e: e["relevance_score"], reverse=True)
    return cleaned


def _heuristic_navigate(candidates, top_k):
    ordered = sorted(
        range(len(candidates)),
        key=lambda index: (
            _NAVIGATE_KIND_ORDER[candidates[index]["kind"]], index
        ),
    )
    ranked = []
    for index in ordered[:top_k]:
        entry = candidates[index]
        sources = ", ".join(
            f"{channel}#{rank}" for channel, rank in entry["sources"]
        ) or "cluster-fallback"
        ranked.append({
            "cluster": entry["cluster"],
            "file": entry["file"],
            "symbol": entry["symbol"],
            "relevance_score": 0.0,
            "rationale": f"sources: {sources}",
        })
    return ranked


def _format_navigate(ranked, intent, source):
    if not ranked:
        return f"No matches for intent '{intent}' (source={source})."
    lines = [f"## Navigate results for '{intent}' (top {len(ranked)}, source={source})\n"]
    for i, entry in enumerate(ranked, 1):
        cluster = entry.get("cluster", "?")
        file_ = entry.get("file")
        symbol = entry.get("symbol")
        score = entry.get("relevance_score", 0.0)
        rationale = entry.get("rationale", "")
        path = cluster
        if file_:
            path += f" / {file_}"
        if symbol:
            path += f" :: {symbol}"
        lines.append(f"{i}. [{score:.2f}] {path}")
        if rationale:
            lines.append(f"   - {rationale}")
    return "\n".join(lines)
