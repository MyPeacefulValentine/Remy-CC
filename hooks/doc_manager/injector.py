#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : injector.py
@Description : Centralized manager for injecting references into CLAUDE.md.
               Ensures idempotency and atomic updates.
@Author      : Logic Indexer Skill
@CreationDate: 2026-01-26
@Version     : 1.2.0
"""

import sys
import os
import json
import re
import sqlite3
from datetime import datetime, timedelta

_SKILL_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "skills", "remy-index"
)
_REPO_SKILL_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "skills", "remy-index")
)
for _skill_dir in (_SKILL_DIR, _REPO_SKILL_DIR):
    if os.path.isdir(_skill_dir) and _skill_dir not in sys.path:
        sys.path.insert(0, _skill_dir)
from retrieval_projection import select_current_summary

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

CLAUDE_MD = "CLAUDE.md"
SETTINGS_FILE = os.path.join(".claude", "settings.local.json")
TIMELINE_FILE = os.path.join(".claude", "history", "timeline.md")
TIMELINE_VIEW_FILE = os.path.join(".claude", "history", "timeline_view.md")
LOGIC_TREE_VIEW_FILE = os.path.join(".claude", "logic_tree_view.md")
DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")


def _open_logic_db(cwd):
    db_path = str(remy_config.load_config(cwd, strict=False).get("REMY_LOGIC_INDEX_DB_PATH"))
    if not os.path.exists(db_path):
        return None
    try:
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=WAL")
        return db
    except Exception:
        return None


# Registry of content to be injected.
# Format: { "tag_name": "relative_file_path" }
# The script injects:
#   <tag_name>
#   @relative_file_path
#   </tag_name>
REGISTRY = {
    "project_structure": ".claude/project_tree.md",
    "history_timeline": ".claude/history/timeline_view.md",
    "logic_tree": ".claude/logic_tree_view.md"
}

TAG_POLICY_MAP = {
    "project_structure": "REMY_PROJECT_TREE_AUTO_INJECT",
    "history_timeline": "REMY_TIMELINE_AUTO_INJECT",
    "logic_tree": "REMY_LOGIC_INDEX_AUTO_INJECT",
}


def load_policy(cwd, env_var_name):
    """Load an injection policy from the effective Remy configuration."""
    return str(remy_config.load_config(cwd, strict=False).get(env_var_name, "ALWAYS"))


def _load_timeline_filter_config():
    config = remy_config.load_config(strict=False)
    mode = str(config.get("REMY_TIMELINE_INJECT_MODE", "all")).lower().strip()
    value = str(config.get("REMY_TIMELINE_INJECT_VALUE", "")).strip()
    return mode, value


def _parse_row_date(row):
    """Extracts the date from a timeline table data row. Returns a date object or None on failure."""
    parts = row.split("|")
    if len(parts) >= 2:
        date_str = parts[1].strip()
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _is_data_row(line):
    """Returns True if the line is a non-empty, non-separator Markdown table row."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    if "| :--- |" in stripped or "| --- |" in stripped:
        return False
    return bool(stripped.replace("|", "").strip())


def _row_passes_date_filter(row, cutoff):
    """Returns True if the row's date >= cutoff, or if the date cannot be parsed."""
    d = _parse_row_date(row)
    return d is None or d >= cutoff


def generate_timeline_view(cwd):
    """Generates timeline_view.md from timeline.md, applying the configured filter.

    Reads TIMELINE_INJECT_MODE (all|last_n|since_date|within_days) and TIMELINE_INJECT_VALUE
    from the environment. Writes a filtered Markdown table to timeline_view.md. When mode is
    not 'all', prepends a meta-info line describing the visible record count. On invalid value,
    falls back to mode='all' and prints a warning to stderr.
    """
    mode, value = _load_timeline_filter_config()
    lang = str(remy_config.load_config(cwd, strict=False).get("REMY_LANG", "en"))

    timeline_path = os.path.join(cwd, TIMELINE_FILE)
    view_path = os.path.join(cwd, TIMELINE_VIEW_FILE)

    if not os.path.exists(timeline_path):
        return

    with open(timeline_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    header_lines = []
    data_rows = []
    header_done = False

    for line in lines:
        if not header_done:
            header_lines.append(line)
            if "| :--- |" in line or "| --- |" in line:
                header_done = True
        else:
            if _is_data_row(line):
                data_rows.append(line)

    total = len(data_rows)
    meta_line = None

    def _meta(msg_zh, msg_en):
        return msg_zh if lang == "zh-CN" else msg_en

    full_hist = _meta(
        "完整历史见 `.claude/history/timeline.md`。",
        "Full history in `.claude/history/timeline.md`."
    )

    if mode == "all":
        filtered = data_rows
    elif mode == "last_n":
        try:
            n = int(value)
        except (ValueError, TypeError):
            print(
                f"[Injector] Warning: TIMELINE_INJECT_VALUE='{value}' is invalid for mode=last_n; "
                "falling back to mode=all.",
                file=sys.stderr
            )
            filtered = data_rows
        else:
            filtered = data_rows[:n]
            meta_line = _meta(
                f"> 注：共 {total} 条记录，当前显示最新 {len(filtered)} 条。{full_hist}\n",
                f"> Note: {total} total records, showing latest {len(filtered)}. {full_hist}\n",
            )
    elif mode == "since_date":
        try:
            cutoff = datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            print(
                f"[Injector] Warning: TIMELINE_INJECT_VALUE='{value}' is not a valid YYYY-MM-DD date; "
                "falling back to mode=all.",
                file=sys.stderr
            )
            filtered = data_rows
        else:
            filtered = [r for r in data_rows if _row_passes_date_filter(r, cutoff)]
            meta_line = _meta(
                f"> 注：共 {total} 条记录，当前显示 {value} 之后的 {len(filtered)} 条。{full_hist}\n",
                f"> Note: {total} total records, showing {len(filtered)} since {value}. {full_hist}\n",
            )
    elif mode == "within_days":
        try:
            n = int(value)
        except (ValueError, TypeError):
            print(
                f"[Injector] Warning: TIMELINE_INJECT_VALUE='{value}' is invalid for mode=within_days; "
                "falling back to mode=all.",
                file=sys.stderr
            )
            filtered = data_rows
        else:
            cutoff = datetime.now().date() - timedelta(days=n)
            filtered = [r for r in data_rows if _row_passes_date_filter(r, cutoff)]
            meta_line = _meta(
                f"> 注：共 {total} 条记录，当前显示最近 {n} 天内的 {len(filtered)} 条（{cutoff} 至今）。{full_hist}\n",
                f"> Note: {total} total records, showing {len(filtered)} within last {n} days (since {cutoff}). {full_hist}\n",
            )
    else:
        print(
            f"[Injector] Warning: Unknown TIMELINE_INJECT_MODE='{mode}'; falling back to mode=all.",
            file=sys.stderr
        )
        filtered = data_rows

    os.makedirs(os.path.dirname(view_path), exist_ok=True)

    with open(view_path, 'w', encoding='utf-8') as f:
        if meta_line:
            f.write(meta_line)
            f.write("\n")
        f.writelines(header_lines)
        f.writelines(filtered)


def generate_logic_tree_view(cwd):
    """Generates logic_tree_view.md directly from SQLite (MCP minimal view)."""
    view_path = os.path.join(cwd, LOGIC_TREE_VIEW_FILE)

    db = _open_logic_db(cwd)
    if not db:
        if os.path.exists(view_path):
            os.remove(view_path)
        return

    try:
        file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if file_count == 0:
            db.close()
            return

        lang = str(remy_config.load_config(cwd, strict=False).get("REMY_LANG", "en"))

        output = []
        output.append("# \U0001f9e0 逻辑索引 (Logic Index)\n")

        meta_row = db.execute("SELECT value FROM meta WHERE key='last_updated'").fetchone()
        updated = meta_row[0] if meta_row else "Unknown"
        output.append(f"> Last Updated: {updated}\n")

        _render_mcp_minimal(db, output, lang)

        os.makedirs(os.path.dirname(view_path), exist_ok=True)
        with open(view_path, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
    finally:
        db.close()


def _render_mcp_minimal(db, output, lang):
    file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    symbol_count = db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    clusters = db.execute(
        "SELECT name, label, file_count FROM clusters ORDER BY file_count DESC"
    ).fetchall()

    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_minimal_template.json")
    try:
        with open(tpl_path, "r", encoding="utf-8") as f:
            tpl = json.load(f)
    except (OSError, json.JSONDecodeError):
        output.append("> MCP available — use query_symbol / query_callers / query_impact")
        return

    lk = "zh-CN" if lang == "zh-CN" else "en"

    output.append(f"> Files: {file_count} | Symbols: {symbol_count} | Clusters: {len(clusters)}\n")
    output.append(tpl["section_title"] + "\n")
    output.append(tpl["intro"][lk] + "\n")

    cols = tpl["table_header"][lk]
    output.append(f"| {cols[0]} | {cols[1]} | {cols[2]} |")
    output.append("| :-- | :-- | :-- |")
    for tool in tpl["tools"]:
        output.append(f"| {tool['scenario'][lk]} | {tool['call']} | {tool['purpose'][lk]} |")

    output.append("")
    output.append(tpl["usage_title"][lk])
    for hint in tpl["usage_hints"][lk]:
        output.append(f"- {hint}")

    output.append("")
    if clusters:
        output.append(tpl["cluster_title"][lk] + "\n")
        cc = tpl["cluster_columns"][lk]
        output.append(f"| {cc[0]} | {cc[1]} | {cc[2]} |")
        output.append("| :-- | :-- | :-- |")

        for name, label, fc in clusters:
            current = select_current_summary(db, "cluster", name)
            short_summary = current.get("short")
            locator = f"{label} ({name})" if label else name
            description = short_summary or ("(no summary)" if lk == "en" else "(暂无描述)")
            output.append(f"| {locator} | {fc} | {description} |")

        output.append("")


def remove_block(content, tag):
    """Removes a specific tag block from content."""
    pattern = f"\\n*<{tag}>.*?<\\/{tag}>\\n*"
    return re.sub(pattern, "", content, flags=re.DOTALL)


def inject_all(cwd):
    """Injects all registered references into CLAUDE.md."""
    generate_timeline_view(cwd)
    generate_logic_tree_view(cwd)

    claude_md_path = os.path.join(cwd, CLAUDE_MD)

    active_registry = REGISTRY.copy()
    removal_list = []

    for tag, env_var in TAG_POLICY_MAP.items():
        policy = load_policy(cwd, env_var)
        if policy != "ALWAYS" and tag in active_registry:
            del active_registry[tag]
            removal_list.append(tag)

    if not os.path.exists(claude_md_path):
        with open(claude_md_path, 'w', encoding='utf-8') as f:
            f.write("# System Context\n\n")

    with open(claude_md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    new_content = content
    changes_made = False

    for tag in removal_list:
        if f"<{tag}>" in new_content:
            new_content = remove_block(new_content, tag)
            changes_made = True

    for tag, rel_path in active_registry.items():
        ref_line = f"@{rel_path}"

        if ref_line in new_content:
            continue

        prefix = "\n\n" if not new_content.endswith("\n\n") else ("\n" if not new_content.endswith("\n\n") else "")

        if f"<{tag}>" in new_content:
            # Tag exists but points to a stale path; replace the entire block.
            new_content = remove_block(new_content, tag)
            prefix = "\n\n" if not new_content.endswith("\n\n") else ("\n" if not new_content.endswith("\n\n") else "")

        block = f"{prefix}<{tag}>\n\n{ref_line}\n\n</{tag}>\n"
        new_content += block
        changes_made = True

    if changes_made:
        new_content = re.sub(r'\n{3,}', '\n\n', new_content)

        with open(claude_md_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        policy_summary = ", ".join(f"{t}={load_policy(cwd, e)}" for t, e in TAG_POLICY_MAP.items())
        print(f"[Injector] Updated {CLAUDE_MD} ({policy_summary})")
    else:
        policy_summary = ", ".join(f"{t}={load_policy(cwd, e)}" for t, e in TAG_POLICY_MAP.items())
        print(f"[Injector] No changes needed for {CLAUDE_MD} ({policy_summary})")


def main():
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding='utf-8')
    cwd = os.getcwd()
    inject_all(cwd)


if __name__ == "__main__":
    main()
