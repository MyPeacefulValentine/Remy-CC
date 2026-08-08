#!/usr/bin/env python3
"""Query implementations for the remy-index MCP server."""
import difflib
import hashlib
import json
import sys
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from index_mcp_common import (
    DB_FILE_DEFAULT,
    _DB_NOT_FOUND,
    _DB_OVERRIDE,
    _IMPACT_DIR,
    _QUERY_CONFIG,
    _config,
    _config_values,
    _open_db,
    _query_scoped,
    database_override,
    get_latest_summary,
)
from impact import (
    bfs_callers as _bfs_callers,
    bfs_callees as _bfs_callees,
    collect_file_symbols,
    get_layer,
    get_line_range,
)
from struct_scan import tokenize_symbol
from retrieval_projection import select_current_summary
import remy_config
from index_mcp_graph import (
    _IMPACT_LABELS_PER_LEVEL,
    _bfs_callers_ambiguous,
    _bfs_callees_ambiguous,
    _bidir_bfs,
    _format_bfs_result,
    _format_flow,
    _format_impact_result,
    _impact_level_files,
    _load_graph,
    _reconstruct_path,
    _resolve_flow_symbol,
    query_callees_impl,
    query_callers_impl,
    query_flow_impl,
    query_impact_impl,
)


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
        from llm_client import LlmClient
    except ImportError:
        return None

    def _call(prompt):
        return LlmClient().call(prompt)

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
