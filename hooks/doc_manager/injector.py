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

CLAUDE_MD = "CLAUDE.md"
SETTINGS_FILE = os.path.join(".claude", "settings.local.json")
TIMELINE_FILE = os.path.join(".claude", "history", "timeline.md")
TIMELINE_VIEW_FILE = os.path.join(".claude", "history", "timeline_view.md")
LOGIC_TREE_VIEW_FILE = os.path.join(".claude", "logic_tree_view.md")
SELECTION_FILE = os.path.join(".claude", "logic_inject_selection.json")
DB_FILE_DEFAULT = os.path.join(".claude", "logic_index.db")


def _load_nav_tier_config():
    nav_full_max = 200
    nav_cluster_max = 2000
    try:
        nav_full_max = int(os.environ.get("NAV_TIER_FULL_MAX", 200))
    except (ValueError, TypeError):
        pass
    try:
        nav_cluster_max = int(os.environ.get("NAV_TIER_CLUSTER_MAX", 2000))
    except (ValueError, TypeError):
        pass
    if nav_full_max < 0 or nav_cluster_max < 0:
        return 200, 2000
    if nav_full_max > nav_cluster_max:
        return 200, 2000
    return nav_full_max, nav_cluster_max


def _get_injection_density(file_count):
    nav_full_max, nav_cluster_max = _load_nav_tier_config()
    if file_count <= nav_full_max:
        return "full"
    if file_count <= nav_cluster_max:
        return "cluster"
    return "cluster_summary"


def _open_logic_db(cwd):
    db_rel = os.environ.get("LOGIC_INDEX_DB_PATH", DB_FILE_DEFAULT)
    db_path = os.path.join(cwd, db_rel)
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
    "project_structure": "PROJECT_TREE_AUTO_INJECT",
    "history_timeline": "TIMELINE_AUTO_INJECT",
    "logic_tree": "LOGIC_INDEX_AUTO_INJECT",
}


def load_policy(cwd, env_var_name):
    """Loads injection policy for a given env var from environment or settings.local.json."""
    env_policy = os.environ.get(env_var_name)
    if env_policy:
        return env_policy

    settings_path = os.path.join(cwd, SETTINGS_FILE)
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                value = data.get("env", {}).get(env_var_name)
                if value is not None:
                    return value
        except Exception:
            pass

    return "ALWAYS"


def _load_timeline_filter_config():
    """Returns (mode, value) from TIMELINE_INJECT_MODE and TIMELINE_INJECT_VALUE env vars."""
    mode = os.environ.get("TIMELINE_INJECT_MODE", "all").lower().strip()
    value = os.environ.get("TIMELINE_INJECT_VALUE", "").strip()
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
    lang = os.environ.get("REMY_LANG", "en")

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
    """Generates logic_tree_view.md directly from SQLite, with cluster-based navigation."""
    view_path = os.path.join(cwd, LOGIC_TREE_VIEW_FILE)
    selection_path = os.path.join(cwd, SELECTION_FILE)

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

        selected_files = None
        if os.path.exists(selection_path):
            try:
                with open(selection_path, "r", encoding="utf-8") as f:
                    selection = json.load(f)
                selected_files = set(
                    p.replace("\\", "/") for p in selection.get("selected_files", [])
                )
            except (json.JSONDecodeError, OSError):
                pass

        density = _get_injection_density(file_count)
        lang = os.environ.get("REMY_LANG", "en")

        icon_map = {
            "class": "C", "function": "f", "struct": "S", "enum": "E",
            "typedef": "T", "type_alias": "T", "macro": "M",
            "namespace": "N", "interface": "I",
        }

        output = []
        output.append("# \U0001f9e0 逻辑索引 (Logic Index)\n")

        meta_row = db.execute("SELECT value FROM meta WHERE key='last_updated'").fetchone()
        updated = meta_row[0] if meta_row else "Unknown"
        output.append(f"> Last Updated: {updated}\n")
        output.append("> **Symbol Types**: `[C]` Class | `[f]` Function | `[S]` Struct | `[E]` Enum | `[T]` Typedef/TypeAlias | `[M]` Macro | `[N]` Namespace | `[I]` Interface")
        output.append("> **Tags**: `[Doc]` From Docstring/Doxygen | `[Source]` Data Source | `[Sink]` Data Sink | `[Util]` Utility | `[Test]` Test\n")

        if density == "full":
            _render_full(db, output, selected_files, file_count, lang, icon_map)
        elif density == "cluster":
            _render_cluster(db, output, selected_files, file_count, lang, icon_map)
        else:
            _render_cluster_summary(db, output, lang)

        os.makedirs(os.path.dirname(view_path), exist_ok=True)
        with open(view_path, "w", encoding="utf-8") as f:
            f.write("\n".join(output))
    finally:
        db.close()


def _render_full(db, output, selected_files, file_count, lang, icon_map):
    query = "SELECT path, layer, imports FROM files ORDER BY layer, path"
    files = db.execute(query).fetchall()

    if selected_files is not None:
        total = len(files)
        files = [(p, l, i) for p, l, i in files if p in selected_files]
        if len(files) < total:
            output.insert(4, _meta_line(lang, len(files), total))

    current_layer = None
    for path, layer, imports_json in files:
        if layer != current_layer:
            current_layer = layer
            output.append(f"## \U0001f3d7️ {layer}")

        output.append(f"### \U0001f4c4 `{path}`")
        if imports_json:
            try:
                imports = json.loads(imports_json)
                if imports:
                    output.append(f"> Imports: {', '.join(imports)}")
            except (json.JSONDecodeError, TypeError):
                pass

        symbols = db.execute(
            "SELECT name, type, args, summary FROM symbols WHERE file_path = ? ORDER BY lineno",
            (path,)
        ).fetchall()
        for name, sym_type, args, summary in symbols:
            icon = icon_map.get(sym_type, "?")
            display_name = f"{name}{args}" if args else name
            desc = summary or "No summary"
            output.append(f"- **[{icon}]** `{display_name}`: {desc}")
        output.append("")


def _render_cluster(db, output, selected_files, file_count, lang, icon_map):
    clusters = db.execute(
        "SELECT id, name, label, entry_symbols, file_count FROM clusters ORDER BY name"
    ).fetchall()

    if not clusters:
        _render_full(db, output, selected_files, file_count, lang, icon_map)
        return

    if selected_files is not None:
        total_files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        shown = len(selected_files)
        if shown < total_files:
            output.insert(4, _meta_line(lang, shown, total_files))

    output.append("## 项目功能拓扑\n")
    for cluster_id, name, label, entry_json, fc in clusters:
        try:
            entries = json.loads(entry_json)
        except (json.JSONDecodeError, TypeError):
            entries = []

        display_label = label or name
        output.append(f"### {display_label} ({fc} files)")

        entry_lines = []
        for qualified in entries[:5]:
            if "::" in qualified:
                fpath, sym_name = qualified.split("::", 1)
                row = db.execute(
                    "SELECT args FROM symbols WHERE file_path = ? AND name = ?",
                    (fpath, sym_name)
                ).fetchone()
                sig = row[0] if row and row[0] else "()"
                entry_lines.append(f"`{sym_name}{sig}`")
            else:
                entry_lines.append(f"`{qualified}`")
        if entry_lines:
            output.append(f"入口: {', '.join(entry_lines)}")

        member_files = db.execute(
            "SELECT file_path FROM cluster_members WHERE cluster_id = ? ORDER BY file_path",
            (cluster_id,)
        ).fetchall()
        if selected_files is not None:
            member_files = [(fp,) for (fp,) in member_files if fp in selected_files]

        for (fp,) in member_files:
            symbols = db.execute(
                "SELECT name, type, args, summary FROM symbols WHERE file_path = ? ORDER BY lineno",
                (fp,)
            ).fetchall()
            if symbols:
                output.append(f"#### `{fp}`")
                for sym_name, sym_type, args, summary in symbols:
                    icon = icon_map.get(sym_type, "?")
                    display_name = f"{sym_name}{args}" if args else sym_name
                    desc = summary or ""
                    if desc:
                        output.append(f"- **[{icon}]** `{display_name}`: {desc}")
                    else:
                        output.append(f"- **[{icon}]** `{display_name}`")

        output.append("")

    output.append("> 查询任意符号签名: query_symbol(\"函数名\") | 影响分析: query_impact([\"文件\"])")


def _render_cluster_summary(db, output, lang):
    clusters = db.execute(
        "SELECT name, label, entry_symbols, file_count FROM clusters ORDER BY file_count DESC"
    ).fetchall()

    if not clusters:
        output.append("(No clusters detected)")
        return

    output.append("## 项目功能拓扑 (摘要)\n")
    for name, label, entry_json, fc in clusters:
        display = label or name
        output.append(f"- **{display}** ({fc} files)")

    output.append("")
    output.append("> 查询任意符号签名: query_symbol(\"函数名\") | 影响分析: query_impact([\"文件\"])")


def _meta_line(lang, shown, total):
    if lang == "zh-CN":
        return "> 注：逻辑索引已过滤，当前显示 {}/{} 个文件。使用 `remy-cc logic-scope` 调整范围。\n\n".format(shown, total)
    return "> Note: Logic index filtered, showing {}/{} files. Use `remy-cc logic-scope` to adjust.\n\n".format(shown, total)


def detect_new_logic_files(cwd):
    """Returns file paths present in logic_index.db but absent from selection.json known_files."""
    selection_path = os.path.join(cwd, SELECTION_FILE)

    if not os.path.exists(selection_path):
        return []

    db = _open_logic_db(cwd)
    if not db:
        return []

    try:
        with open(selection_path, "r", encoding="utf-8") as f:
            selection = json.load(f)
    except (json.JSONDecodeError, OSError):
        db.close()
        return []

    known = set(selection.get("known_files", []))
    if not known:
        db.close()
        return []

    db_files = {r[0] for r in db.execute("SELECT path FROM files")}
    db.close()
    return sorted(db_files - known)


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
    sys.stdout.reconfigure(encoding='utf-8')
    cwd = os.getcwd()
    inject_all(cwd)


if __name__ == "__main__":
    main()
