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
from datetime import datetime, timedelta

CLAUDE_MD = "CLAUDE.md"
SETTINGS_FILE = os.path.join(".claude", "settings.local.json")
TIMELINE_FILE = os.path.join(".claude", "history", "timeline.md")
TIMELINE_VIEW_FILE = os.path.join(".claude", "history", "timeline_view.md")
LOGIC_TREE_FILE = os.path.join(".claude", "logic_tree.md")
LOGIC_TREE_VIEW_FILE = os.path.join(".claude", "logic_tree_view.md")
SELECTION_FILE = os.path.join(".claude", "logic_inject_selection.json")
CACHE_FILE = os.path.join(".claude", "logic_index.json")

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
    """Generates logic_tree_view.md by filtering logic_tree.md based on selection.json."""
    tree_path = os.path.join(cwd, LOGIC_TREE_FILE)
    view_path = os.path.join(cwd, LOGIC_TREE_VIEW_FILE)
    selection_path = os.path.join(cwd, SELECTION_FILE)

    if not os.path.exists(tree_path):
        return

    if not os.path.exists(selection_path):
        with open(tree_path, "r", encoding="utf-8") as src:
            content = src.read()
        with open(view_path, "w", encoding="utf-8") as dst:
            dst.write(content)
        return

    try:
        with open(selection_path, "r", encoding="utf-8") as f:
            selection = json.load(f)
    except (json.JSONDecodeError, OSError):
        with open(tree_path, "r", encoding="utf-8") as src:
            content = src.read()
        with open(view_path, "w", encoding="utf-8") as dst:
            dst.write(content)
        return

    selected_files = set(
        p.replace("\\", "/") for p in selection.get("selected_files", [])
    )

    with open(tree_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    header_lines = []
    layers = []
    current_layer = None
    current_file = None
    in_header = True

    for line in lines:
        if line.startswith("## 🏗️"):
            in_header = False
            if current_file is not None and current_layer is not None:
                current_layer["files"].append(current_file)
                current_file = None
            if current_layer is not None:
                layers.append(current_layer)
            current_layer = {"header": line, "files": []}
            continue

        if line.startswith("### 📄"):
            in_header = False
            if current_file is not None and current_layer is not None:
                current_layer["files"].append(current_file)
            if current_layer is None:
                current_layer = {"header": "## 🏗️ Uncategorized\n", "files": []}
            path_match = re.search(r"`([^`]+)`", line)
            path = path_match.group(1).replace("\\", "/") if path_match else ""
            current_file = {"header": line, "path": path, "body": []}
            continue

        if in_header:
            header_lines.append(line)
        elif current_file is not None:
            current_file["body"].append(line)

    if current_file is not None and current_layer is not None:
        current_layer["files"].append(current_file)
    if current_layer is not None:
        layers.append(current_layer)

    total_files = sum(len(layer["files"]) for layer in layers)

    if not selected_files:
        lang = os.environ.get("REMY_LANG", "en")
        meta = _meta_line(lang, 0, total_files)
        with open(view_path, "w", encoding="utf-8") as f:
            f.write(meta)
            f.writelines(header_lines)
        return

    filtered_layers = []
    selected_count = 0
    for layer in layers:
        kept = [fb for fb in layer["files"] if fb["path"] in selected_files]
        if kept:
            filtered_layers.append({"header": layer["header"], "files": kept})
            selected_count += len(kept)

    lang = os.environ.get("REMY_LANG", "en")
    output = []
    if selected_count < total_files:
        output.append(_meta_line(lang, selected_count, total_files))
    output.extend(header_lines)
    for layer in filtered_layers:
        output.append(layer["header"])
        for fb in layer["files"]:
            output.append(fb["header"])
            output.extend(fb["body"])

    os.makedirs(os.path.dirname(view_path), exist_ok=True)
    with open(view_path, "w", encoding="utf-8") as f:
        f.writelines(output)


def _meta_line(lang, shown, total):
    if lang == "zh-CN":
        return "> 注：逻辑索引已过滤，当前显示 {}/{} 个文件。完整索引见 `.claude/logic_tree.md`。\n\n".format(shown, total)
    return "> Note: Logic index filtered, showing {}/{} files. Full index in `.claude/logic_tree.md`.\n\n".format(shown, total)


def detect_new_logic_files(cwd):
    """Returns file paths present in logic_index.json but absent from selection.json known_files."""
    selection_path = os.path.join(cwd, SELECTION_FILE)
    cache_path = os.path.join(cwd, CACHE_FILE)

    if not os.path.exists(selection_path) or not os.path.exists(cache_path):
        return []

    try:
        with open(selection_path, "r", encoding="utf-8") as f:
            selection = json.load(f)
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    known = set(selection.get("known_files", []))
    if not known:
        return []

    cache_files = set(k for k in cache if k != "_meta")
    return sorted(cache_files - known)


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
