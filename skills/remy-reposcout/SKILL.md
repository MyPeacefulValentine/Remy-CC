---
name: remy-reposcout
description: Analyzes a GitHub repository. Stage 1: Fetches metadata (README, Size) using GH CLI to assess complexity. Stage 2: Clones to a temporary sandbox for deep inspection upon user confirmation.
disable-model-invocation: true
allowed-tools: Bash, Glob, Grep, Read
---

# Repository Audit Skill

This skill allows you to safely inspect GitHub repositories without polluting your main workspace. It operates in two stages to prevent unnecessary cloning of massive repositories.

**Requirements**:
- `git`
- `gh` (GitHub CLI) - Must be authenticated (`gh auth login`)

## Usage

`/remy-reposcout <url>`

## Workflow

### Stage 1: Reconnaissance (Metadata Analysis)
1.  **Validate URL**: Ensure the input is a valid GitHub URL.
2.  **Fetch Metadata**: Execute `gh repo view <url> --json description,stargazerCount,diskUsage,defaultBranchRef`.
3.  **Fetch README**:
    *   Extract the `owner/repo` from the URL.
    *   Execute `gh api repos/<owner>/<repo>/readme --jq .content | python -c "import sys, base64; print(base64.b64decode(sys.stdin.read().strip()).decode('utf-8', errors='replace'))"`.
4.  **Report & Ask**: Present a summary including the full README content.
    *   **CRITICAL CHECK**: If `diskUsage` > 500MB, WARN the user.
    *   **YOU MUST** Ask the user if they want to proceed with a full clone (Stage 2).
    *   **STOP** generation here. Wait for user confirmation.

### Stage 2: Deep Inspection (Sandboxed Clone)
**Only execute this after user confirmation.**

1.  **Run Runner Script**: Execute the bundled Python script. This script performs a secondary size check, clones the repo to `%TEMP%`, and generates a structure report.
2.  **Analyze**: Review the generated file tree and tech stack.
3.  **Interact**: Use standard tools (`Glob`, `Grep`, `Read`) to explore the files in the temp directory.
4.  **Cleanup**: When the user is done or the session ends, **YOU MUST** delete the temp directory using the command provided in the report.

## Execution Instructions

To run the analysis script (Stage 2), execute:

```bash
# Windows (PowerShell) - Adapt path as needed
# Note: The script includes a 500MB safety limit. Use --force to override.
python ~/.claude/skills/remy-reposcout/scripts/audit_runner.py <repo_url> [--force]
```
