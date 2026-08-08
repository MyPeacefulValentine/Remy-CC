#!/usr/bin/env python3
"""Fact queries for the remy-index MCP server: symbols, files, clusters, patterns."""
import json

from index_mcp_common import (
    _DB_NOT_FOUND,
    _config_values,
    _open_db,
    _query_scoped,
    get_latest_summary,
)
from impact import collect_file_symbols, get_layer


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
