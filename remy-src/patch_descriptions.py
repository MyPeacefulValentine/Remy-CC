#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path

DESCRIPTIONS_FILE = "skill_descriptions.json"
MAX_FRONTMATTER_LINES = 8
DESC_PATTERN = re.compile(r"^description:\s*(.*)$")


def patch(claude_home: Path, lang: str) -> None:
    desc_path = claude_home / "skills" / DESCRIPTIONS_FILE
    if not desc_path.exists():
        print(f"Warning: {desc_path} not found, skipping.", file=sys.stderr)
        return

    try:
        with open(desc_path, "r", encoding="utf-8") as f:
            descriptions = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: failed to read {desc_path}: {e}", file=sys.stderr)
        return

    skills_dir = claude_home / "skills"
    for skill_name, lang_map in descriptions.items():
        skill_md = skills_dir / skill_name / "SKILL.md"
        if not skill_md.exists():
            continue

        desc = lang_map.get(lang) or lang_map.get("en")
        if not desc:
            continue

        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue

        changed = False
        for i in range(min(MAX_FRONTMATTER_LINES, len(lines))):
            if DESC_PATTERN.match(lines[i]):
                new_line = f"description: {desc}\n"
                if lines[i] != new_line:
                    lines[i] = new_line
                    changed = True
                break

        if changed:
            with open(skill_md, "w", encoding="utf-8") as f:
                f.writelines(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude-home", required=True)
    parser.add_argument("--lang", required=True)
    args = parser.parse_args()
    patch(Path(args.claude_home), args.lang)


if __name__ == "__main__":
    main()
