#!/usr/bin/env python3
"""Text retrieval for the remy-index MCP server: exact, prefix, BM25, fuzzy."""
import difflib
import sqlite3
import unicodedata
from dataclasses import dataclass

from index_mcp_common import (
    _DB_NOT_FOUND,
    _config_values,
    _open_db,
    _query_scoped,
)
from impact import get_layer
from symbol_names import tokenize_symbol


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
