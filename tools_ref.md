# Tool and Project Reference

- Follow each tool's documented input and failure contract. Prefer dedicated file/search tools when they fit; do not reproduce their behavior with shell commands without a reason.
- If an Edit reports that its source string was not found, inspect the target location and intended difference. The replacement text already appearing somewhere in the file is not proof of success. Correct a whitespace mismatch or use a read-modify-write within the approved scope; do not overwrite unread content.
- Use Conventional Commits (`<type>(<scope>): <subject>`). Commit, push, destructive Git operations, releases, and other external changes require authorization that covers those operations. Use `gh` for GitHub metadata and authorized writes.
- Keep current project documentation and directly referenced rules consistent with changed behavior. Preserve historical experiment and audit records.
- Project naming, runtime setup, and environment activation come from project configuration or explicit instructions. Do not assume a global Conda/Mamba environment or silently rewrite requested paths.
