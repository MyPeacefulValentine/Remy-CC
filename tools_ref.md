# Technical Execution Reference

**Strictly adhere to the protocols defined in the specialized Skills.** Do not rely on defaults.

*   **File Operations**: **Mandatory**:
    *   **Read-Modify-Read**: Pre-Read (confirm context) → `Edit` → Post-Read (verify change). All steps silent.
    *   **Edit Failure Path** ("String not found"): (1) Grep `new_string`—found → abort as success; (2) re-check `old_string` for whitespace/indent mismatches, retry once; (3) request permission for full Read-Modify-Write-Read.
*   **Git Workflow**: See `skills/git-workflow`. **Mandatory**: Conventional Commits, Dangerous Ops Confirmation.
*   **Debugging**: See `skills/systematic-debugging`. **Mandatory**: Root Cause Analysis -> Hypothesis -> Fix.
*   **TDD**: See `skills/test-driven-development`. **Mandatory**: RED -> GREEN -> REFACTOR.
*   **Tool Guide**: See `skills/tool-guide`. **Reference**: MCP Tool Selection Strategy.
*   **Doc Updater**: See `skills/doc-updater`. **Mandatory**: Keep `CLAUDE.md` core docs in sync with code changes.
*   **Update Tree**: See `skills/update-tree`. **Mandatory**: Keep `.claude/project_tree.md` fresh after batch ops.
*   **Update Logic Index**: See `skills/update-logic-index`. **Mandatory**: Update `.claude/logic_tree.md` after major refactors.
*   **Hooks System**:
    *   `pre_tool_guard.py`: Enforces Path Security, Code Hygiene, and Environment Safety.
    *   `enforcer_hook.py`: Enforces persona constraints and UserPromptSubmit protocols.
    *   `lifecycle_hook.py`: Manages SessionStart tree injection and update reminders.
*   **GitHub CLI Integration**: Verified `gh` installation. **Mandatory**: Use `gh` for repository management, issue tracking, and PR operations. **Safety Constraint**: Only use `gh` for **Read-Only** metadata retrieval unless **explicit confirmation** is provided for **Destructive Actions**.
*   **Shell Tools**: `Bash` uses POSIX syntax; `PowerShell` uses PS 7+ syntax (Windows environments). Environment injection handled by `pre_tool_guard.py`.
*   **Task & Scheduling Tools**: `TaskCreate`/`TaskUpdate`/`TaskStop` for session task tracking. `Monitor` for event streaming. `CronCreate`/`CronDelete` for scheduled prompts. `WebFetch`/`WebSearch` for external information retrieval (read-only).
