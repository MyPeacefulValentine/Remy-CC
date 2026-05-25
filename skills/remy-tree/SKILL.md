---
name: remy-tree
description: Refresh the project directory tree snapshot (.claude/project_tree.md). Use after file operations that alter structure.
allowed-tools: Bash, Read
disable-model-invocation: false
---

# Update Project Tree

Use this skill to force a refresh of the project structure file and immediately load it into context.

## Proactive Usage Policy
**You SHOULD proactively invoke this skill when:**
1. You have completed a batch of file operations (e.g., creating multiple new files, extensive refactoring).
2. You have performed destructive actions (e.g., `rm`, `mv`) that alter the directory structure.
3. You are starting a new task that heavily relies on accurate path knowledge, and suspect the current context is stale.

## Implementation Steps
1. Run the generation script using the command below.
2. If the command succeeds, **immediately use the `Read` tool** to read `.claude/project_tree.md`.
3. Confirm completion to the user.

```bash
python -c "import os, subprocess, sys; script = os.path.expanduser('~/.claude/hooks/tree_system/generate_smart_tree.py'); subprocess.run([sys.executable, script], check=True)"
```
