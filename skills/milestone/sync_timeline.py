#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : sync_timeline.py
@Description : Synchronizes the summary from the latest milestone report back to the timeline index.
               Extracts the content of "1. 工作摘要 (Summary)" and updates timeline.md.
@Author      : MyPeacefulValentine
@CreationDate: 2026-01-27
"""

import os
import re
import subprocess
import sys

# Paths
HISTORY_DIR = ".claude/history"
REPORTS_DIR = os.path.join(HISTORY_DIR, "reports")
TIMELINE_FILE = os.path.join(HISTORY_DIR, "timeline.md")

# Summary Bound
SUMMARY_BOUND = 250  # Maximum length of summary to keep in timeline table

def get_latest_report():
    """Finds the most recently created markdown file in the reports directory."""
    if not os.path.exists(REPORTS_DIR):
        return None

    files = [f for f in os.listdir(REPORTS_DIR) if f.endswith(".md")]
    if not files:
        return None

    # Sort by filename (which contains timestamp) descending
    files.sort(reverse=True)
    return os.path.join(REPORTS_DIR, files[0])

def extract_summary(report_path):
    """Extracts the summary text from Section 1 of the report."""
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Regex to find content between "## 1. 工作摘要 (Summary)" and the next "##" header
        # Handles potential newlines and whitespace
        pattern = r"## 1\. (?:工作摘要 \(Summary\)|Summary)\s*\n(.*?)\n\s*##"
        match = re.search(pattern, content, re.DOTALL)

        if match:
            summary = match.group(1).strip()
            # Remove [AI TODO: ...] placeholders if present (simple check)
            if "[AI TODO:" in summary:
                # If it's just the placeholder, return None or a warning
                if summary.startswith("[AI TODO:") and len(summary) < 100:
                   return None

            # Compress to single line for table compatibility
            summary_inline = " ".join(summary.split())
            return summary_inline

        return None
    except Exception as e:
        print(f"Error reading report: {e}")
        return None

def update_timeline(report_filename, summary_text):
    """Updates the timeline.md file with the new summary."""
    if not os.path.exists(TIMELINE_FILE):
        print(f"Error: Timeline file not found at {TIMELINE_FILE}")
        return False

    with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Extract ID from filename (e.g. 20260127_011336.md -> M_20260127_011336)
    milestone_id = "M_" + os.path.splitext(os.path.basename(report_filename))[0]

    updated_lines = []
    found = False

    for line in lines:
        if milestone_id in line and "|" in line:
            # Parse the table row: | Date | ID | Link | Summary |
            parts = line.split("|")
            if len(parts) >= 5: # Leading empty string + 4 columns
                # Replace the Summary column (index 4)
                # Truncate if too long (e.g. 200 chars) to keep table readable
                display_summary = summary_text[:SUMMARY_BOUND] + "..." if len(summary_text) > SUMMARY_BOUND else summary_text
                parts[4] = f" {display_summary} "
                new_line = "|".join(parts) + "\n"
                # Ensure only one newline
                new_line = new_line.replace("\n\n", "\n")
                updated_lines.append(new_line)
                found = True
                print(f"Updated summary for {milestone_id}")
            else:
                updated_lines.append(line)
        else:
            updated_lines.append(line)

    if found:
        with open(TIMELINE_FILE, "w", encoding="utf-8") as f:
            f.writelines(updated_lines)
        return True
    else:
        print(f"Warning: Milestone ID {milestone_id} not found in timeline.")
        return False

def main():
    # Force UTF-8
    sys.stdout.reconfigure(encoding='utf-8')

    latest_report = get_latest_report()
    if not latest_report:
        print("No reports found.")
        return

    print(f"Processing latest report: {latest_report}")

    summary = extract_summary(latest_report)
    if not summary:
        print("Could not extract summary. Ensure '## 1. Summary' (or '## 1. 工作摘要 (Summary)') is filled and not just a placeholder.")
        return

    if update_timeline(latest_report, summary):
        print("Timeline updated successfully.")
        try:
            current_script_dir = os.path.dirname(os.path.abspath(__file__))
            injector_path = os.path.abspath(os.path.join(current_script_dir, "../../hooks/doc_manager/injector.py"))
            if os.path.exists(injector_path):
                subprocess.run([sys.executable, injector_path], cwd=os.getcwd(), check=False)
            else:
                print(f"Warning: Injector not found at {injector_path}")
        except Exception as e:
            print(f"Warning: Failed to regenerate timeline view: {e}")
    else:
        print("Failed to update timeline.")

if __name__ == "__main__":
    main()
