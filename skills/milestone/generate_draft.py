#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : generate_draft.py
@Description : Standalone script to generate a milestone draft and update the timeline.
               Designed to be called manually by the agent via /milestone skill.
@Author      : MyPeacefulValentine
@CreationDate: 2026-01-26
"""

import sys
import os
import subprocess
from datetime import datetime

# Relative paths from project root
HISTORY_DIR = ".claude/history"
REPORTS_DIR = os.path.join(HISTORY_DIR, "reports")
TIMELINE_FILE = os.path.join(HISTORY_DIR, "timeline.md")
CLAUDE_MD = "CLAUDE.md"

TIMELINE_PREAMBLE = {
    "zh-CN": "项目历史里程碑索引。详细内容请参考关联报告。",
    "en": "Project milestone history index. See linked reports for details.",
}

def ensure_structure():
    """Ensures the history directory structure exists."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    if not os.path.exists(TIMELINE_FILE):
        os.makedirs(os.path.dirname(TIMELINE_FILE), exist_ok=True)
        lang = os.environ.get("REMY_LANG", "en")
        preamble = TIMELINE_PREAMBLE.get(lang, TIMELINE_PREAMBLE["en"])
        with open(TIMELINE_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Project Timeline\n\n{preamble}\n\n---\n")

def get_recent_summary():
    """Captures a brief summary of staged/recent changes."""
    try:
        res = subprocess.run(
            ["git", "diff", "--staged", "--stat"],
            capture_output=True,
            encoding='utf-8',
            errors='replace'
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

        res = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            capture_output=True,
            encoding='utf-8',
            errors='replace'
        )
        return res.stdout.strip() if res.returncode == 0 else "Routine update"
    except:
        return "Manual milestone"

def main():
    # Force UTF-8
    sys.stdout.reconfigure(encoding='utf-8')

    cwd = os.getcwd()
    ensure_structure()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_text = get_recent_summary().split('\n')[0][:60]

    report_filename = f"{timestamp}.md"
    report_path = os.path.join(REPORTS_DIR, report_filename)

    # 1. Generate Report Template
    lang = os.environ.get("REMY_LANG", "en")
    suffix = "zh" if lang == "zh-CN" else "en"
    template_path = os.path.join(os.path.dirname(__file__), f"report_template_{suffix}.md")
    if not os.path.exists(template_path):
        template_path = os.path.join(os.path.dirname(__file__), "report_template_en.md")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()
    else:
        # Fallback if template missing (should not happen in normal flow)
        template_content = """# Milestone: {summary_text}
> Date: {date_time} | ID: {timestamp}

## 1. Summary
[AI TODO: Summary missing]

## Implementation Details
{git_summary}
"""

    report_content = template_content.replace("{summary_text}", summary_text) \
                                     .replace("{date_time}", datetime.now().strftime('%Y-%m-%d %H:%M:%S')) \
                                     .replace("{timestamp}", timestamp) \
                                     .replace("{git_summary}", get_recent_summary())

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    # 2. Update Timeline (Prepending to Table)
    with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Define table header
    table_header = [
        "| 日期 (Date) | 里程碑 (Milestone) | 报告链接 (Report Link) | 简述 (Summary) |\n",
        "| :--- | :--- | :--- | :--- |\n"
    ]

    # Find where the table starts or create it
    header_end_idx = 0
    has_table = False

    # Simple heuristic to find existing table
    for i, line in enumerate(lines):
        if "| :--- |" in line:
            header_end_idx = i + 1
            has_table = True
            break

    if not has_table:

        try:
            sep_idx = lines.index("---\n") + 1
        except ValueError:
            sep_idx = len(lines)

        preamble = lines[:sep_idx]
        existing_content = lines[sep_idx:] 

        # New structure: Preamble -> Table Header -> New Row -> Old Content (if any, ideally empty or converted)
        final_lines = preamble + table_header
        insert_pos = len(final_lines)
    else:
        # We have a table, insert after the header
        preamble = lines[:header_end_idx]
        rest = lines[header_end_idx:]
        final_lines = preamble
        insert_pos = len(preamble)

    rel_link_path = f"reports/{report_filename}"

    date_str = datetime.now().strftime('%Y-%m-%d')
    milestone_id = f"M_{timestamp}"

    # Format: | Date | Milestone | Link | Summary |
    new_row = f"| {date_str} | {milestone_id} | [📄 View Report]({rel_link_path}) | {summary_text} |\n"

    if has_table:
        final_lines.extend([new_row] + rest)
    else:
        final_lines.append(new_row)

        if not has_table and existing_content:
             final_lines.append("\n<!-- Old History (Pre-Table Format) -->\n")
             final_lines.extend(existing_content)

    with open(TIMELINE_FILE, "w", encoding="utf-8") as f:
        f.writelines(final_lines)

    # Call centralized injector
    try:
        # Resolve injector relative to this script: ../../hooks/doc_manager/injector.py
        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        injector_path = os.path.abspath(os.path.join(current_script_dir, "../../hooks/doc_manager/injector.py"))

        if os.path.exists(injector_path):
            subprocess.run([sys.executable, injector_path], cwd=cwd, check=False)
        else:
            print(f"Warning: Injector not found at {injector_path}")
    except Exception as e:
        print(f"Warning: Failed to run injector: {e}")

    print(f"Draft generated: {report_path}")
    print(f"Timeline updated: {TIMELINE_FILE}")

if __name__ == "__main__":
    main()
