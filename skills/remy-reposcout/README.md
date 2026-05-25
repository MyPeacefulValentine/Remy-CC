# remy-reposcout (Repository Inspection)

remy-reposcout inspects a GitHub repository in a sandboxed temporary directory. It operates in two stages to prevent unnecessary cloning of large repositories.

## When to Use

- Evaluating an unfamiliar repository before integrating or referencing it
- Analyzing a repository's structure, tech stack, and size without polluting the workspace

## Workflow

### Stage 1: Reconnaissance

1. Validates the GitHub URL.
2. Fetches repository metadata via `gh repo view` (description, stars, disk usage, default branch).
3. Fetches and displays the full README content via `gh api`.
4. Reports a summary. If disk usage exceeds 500 MB, warns the user.
5. Asks the user whether to proceed with a full clone.

### Stage 2: Deep Inspection

Executes only after user confirmation.

1. Runs `audit_runner.py`, which clones the repository to a temporary directory, performs a secondary size check (500 MB safety limit, overridable with `--force`), and generates a structure report.
2. Analyzes the file tree and tech stack.
3. Uses standard tools (Glob, Grep, Read) to explore files in the temporary directory.
4. Deletes the temporary directory when inspection is complete.

## Requirements

- `git`
- `gh` (GitHub CLI), authenticated via `gh auth login`

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `scripts/audit_runner.py` | Clone, size check, and structure analysis script |
