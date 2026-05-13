#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : generate_smart_tree.py
@Description : Generates a compact project tree based on .claude/tree_config rules.
               Supports custom depth and file visibility per path.
               Automatically injects reference into CLAUDE.md if missing.
@Author      : MyPeacefulValentine
@CreationDate: 2026-01-24
"""

import os
import sys
import fnmatch
import re

# Constants
CONFIG_FILE = ".claude/tree_config"
OUTPUT_FILE = ".claude/project_tree.md"
CLAUDE_MD = "CLAUDE.md"
DEFAULT_DEPTH = 2
DEFAULT_IF_FILE = False

class TreeGenerator:
    def __init__(self, root_dir):
        if not root_dir:
            raise ValueError("root_dir is required (must be the user's project directory)")
        self.root_dir = os.path.abspath(root_dir)

        self.exclusions = []
        self.inclusions = {}  # path -> {'depth': n, 'if_file': bool}
        self.tree_lines = []

    def parse_config(self):
        """Parses the tree_config file."""
        config_path = os.path.join(self.root_dir, CONFIG_FILE)

        # [NEW] Auto-create config from template if missing
        if not os.path.exists(config_path):
            try:
                # Script is in hooks/tree_system/
                script_dir = os.path.dirname(os.path.abspath(__file__))
                template_path = os.path.join(script_dir, "default_tree_config.template")

                if os.path.exists(template_path):
                    with open(template_path, 'r', encoding='utf-8') as src:
                        content = src.read()

                    # Ensure .claude directory exists
                    os.makedirs(os.path.dirname(config_path), exist_ok=True)

                    with open(config_path, 'w', encoding='utf-8') as dst:
                        dst.write(content)

                    print(f"Initialized default config at {CONFIG_FILE}")
            except Exception as e:
                # Log error but fall back to hardcoded defaults
                print(f"Warning: Failed to initialize default config: {e}", file=sys.stderr)

        if not os.path.exists(config_path):
            # Default configuration if file is still missing
            self.exclusions = [".git", "node_modules", "__pycache__", ".claude_code", ".claude"]
            self.inclusions = {".": {"depth": DEFAULT_DEPTH, "if_file": True}}
            return

        with open(config_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                if line.startswith('!'):
                    self.exclusions.append(line[1:])
                else:
                    parts = line.split()
                    path = parts[0].rstrip('/')
                    depth = DEFAULT_DEPTH
                    if_file = DEFAULT_IF_FILE

                    # Parse arguments
                    i = 1
                    while i < len(parts):
                        if parts[i] == '-depth' and i + 1 < len(parts):
                            try:
                                depth = int(parts[i+1])
                            except ValueError:
                                pass
                            i += 2
                        elif parts[i] == '-if_file' and i + 1 < len(parts):
                            if_file = parts[i+1].lower() == 'true'
                            i += 2
                        else:
                            i += 1

                    # Normalize path
                    if path == '.':
                        path = ''
                    self.inclusions[path] = {"depth": depth, "if_file": if_file}

    def is_excluded(self, path):
        """Checks if a path matches any exclusion pattern."""
        rel_path = os.path.relpath(path, self.root_dir)
        if rel_path == '.':
            return False

        # Normalize for cross-platform (always use forward slashes for matching)
        rel_path = rel_path.replace(os.sep, '/')

        # Check basename for simple matches like ".git" or "node_modules"
        basename = os.path.basename(rel_path)
        is_directory = os.path.isdir(path)

        for pattern in self.exclusions:
            must_be_dir = pattern.endswith('/')
            clean_pattern = pattern.rstrip('/')

            # If pattern ends with /, it only matches directories
            if must_be_dir and not is_directory:
                continue

            # 1. Match against basename (e.g. pattern="node_modules" matches "src/node_modules")
            if fnmatch.fnmatch(basename, clean_pattern):
                return True

            # 2. Match against full relative path (e.g. pattern="src/temp")
            if fnmatch.fnmatch(rel_path, clean_pattern):
                return True

            # 3. Handle globstar-like patterns manually if needed
            if pattern.startswith('**/'):
                suffix = pattern[3:].rstrip('/')
                if fnmatch.fnmatch(basename, suffix):
                    return True

        return False

    def get_inclusion_rule(self, dir_path):
        """Finds the most specific inclusion rule for a directory."""
        rel_path = os.path.relpath(dir_path, self.root_dir)
        if rel_path == '.':
            rel_path = ''

        rel_path = rel_path.replace(os.sep, '/')

        best_match = None
        best_match_len = -1

        for rule_path, config in self.inclusions.items():
            # Check if dir_path is inside rule_path
            # Rule path '' means root
            if rule_path == '':
                if best_match_len < 0:
                    best_match = config
                    best_match_len = 0
                continue

            if rel_path == rule_path or rel_path.startswith(rule_path + '/'):
                if len(rule_path) > best_match_len:
                    best_match = config
                    best_match_len = len(rule_path)

        return best_match

    def generate(self):
        """Generates the tree structure."""
        self.parse_config()
        self.tree_lines = ["# Project Structure"]
        self._walk(self.root_dir, "")
        return "\n".join(self.tree_lines)

    def _walk(self, current_path, prefix):
        """Recursive directory walker."""
        if self.is_excluded(current_path):
            return

        rule = self.get_inclusion_rule(current_path)
        if not rule:
            return
        pass

    # Re-implementing generation with cleaner recursion
    def build_tree(self):
        self.parse_config()
        self.tree_lines = ["<project_tree>"]

        # Start from root
        root_rule = self.inclusions.get('', self.inclusions.get('.', None))
        if not root_rule:
             # Fallback if no root rule
             root_rule = {"depth": DEFAULT_DEPTH, "if_file": DEFAULT_IF_FILE}

        self._recursive_build(self.root_dir, "", root_rule['depth'], root_rule['if_file'])
        self.tree_lines.append("</project_tree>")
        return "\n".join(self.tree_lines)

    def _recursive_build(self, current_path, prefix, current_depth_quota, if_file_enabled):
        try:
            items = sorted(os.listdir(current_path))
        except PermissionError:
            return

        # Filter items
        filtered_items = []
        for item in items:
            full_path = os.path.join(current_path, item)
            if self.is_excluded(full_path):
                continue
            filtered_items.append(item)

        count = len(filtered_items)
        for i, item in enumerate(filtered_items):
            full_path = os.path.join(current_path, item)
            is_last = (i == count - 1)
            is_dir = os.path.isdir(full_path)

            connector = "└── " if is_last else "├── "
            new_prefix = prefix + ("    " if is_last else "│   ")

            rule = self.inclusions.get(os.path.relpath(full_path, self.root_dir).replace(os.sep, '/'))

            if current_depth_quota == -1:
                next_depth = -1
            else:
                next_depth = current_depth_quota - 1

            next_if_file = if_file_enabled

            if rule:
                next_depth = rule['depth']
                next_if_file = rule['if_file']

            # Decision to print
            should_print = False
            if is_dir:
                 should_print = True # Always print dirs if we are here (depth check happens before recursing)
            else:
                 if current_depth_quota == -1:
                     if if_file_enabled:
                         should_print = True
                 elif current_depth_quota > 0:
                     should_print = True
                 elif current_depth_quota == 0 and if_file_enabled:
                     should_print = True

            if should_print:
                # Append trailing slash for directories to distinguish them clearly
                display_item = item + "/" if is_dir else item
                self.tree_lines.append(f"{prefix}{connector}{display_item}")

            # Recurse
            if is_dir:
                if next_depth != 0:
                     self._recursive_build(full_path, new_prefix, next_depth, next_if_file)

def main():
    # Ensure UTF-8 output
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

    root_dir = os.getcwd()
    generator = TreeGenerator(root_dir)

    # Generate tree content
    tree_content = generator.build_tree()

    # Save to file
    output_path = os.path.join(root_dir, OUTPUT_FILE)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(tree_content)

    print(f"Tree generated at {OUTPUT_FILE}")

    # Call centralized injector
    try:
        # Resolve injector relative to this script: ../doc_manager/injector.py
        # Current script is in hooks/tree_system/
        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        injector_path = os.path.abspath(os.path.join(current_script_dir, "../doc_manager/injector.py"))

        if os.path.exists(injector_path):
            # Using sys.executable to ensure same python env
            import subprocess
            subprocess.run([sys.executable, injector_path], cwd=root_dir, check=False)
        else:
            print(f"Warning: Injector not found at {injector_path}")
    except Exception as e:
        print(f"Warning: Failed to run injector: {e}")

if __name__ == "__main__":
    main()
