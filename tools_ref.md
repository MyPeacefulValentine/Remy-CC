# Technical Execution Reference

*   **File Operations**:
    *   **Edit Failure Path** ("String not found"): (1) Grep `new_string`—found → abort as success; (2) re-check `old_string` for whitespace/indent mismatches, retry once; (3) request permission for full Read-Modify-Write.
*   **Git Workflow**: Conventional Commits format (`<type>(<scope>): <subject>`). Dangerous operations (push, reset --hard, clean) require explicit user confirmation.
*   **Doc Sync**: Keep `CLAUDE.md` core docs (`@`-referenced files) in sync with code changes. Verify after structural modifications.
*   **GitHub CLI**: Use `gh` for repository management, issue tracking, and PR operations. Only use `gh` for **read-only** metadata retrieval unless **explicit confirmation** is provided for destructive actions.
