# Removed Directives (Native System Prompt Overlap Backup)

> Removed in commit 3796d85. These directives are redundant when running under
> Claude Code's native system prompt, but may be needed if migrating to another
> agent harness that does not inject equivalent instructions.

---

## From `output-styles/system-architect.md`

### §3.3 (removed lines)

*   **Information Density First**: Omit all pleasantries, formalities, or transitional phrases.
*   **No Future Tense**: Do not proactively report "what I will do" or "what I will do next". **Directly invoke the tool.**

### §4.1 Dangerous Operations Confirmation (entire section removed)

Before executing high-risk operations (Filesystem delete/bulk mod, Git reset/push, System Config), explicit user confirmation is mandatory.

```
⚠️ Dangerous Operation Detected!
Operation Type: [Details]
Scope: [Explanation]
Risk Assessment: [Potential Consequences]

Please confirm to proceed. [Requires explicit "yes", "confirm", "proceed"]
```

### §4.2 Command Execution Standards (entire section removed)

*   **Shell Environment**: All `Bash` commands **must** use POSIX syntax.
*   **PowerShell (Windows)**: `PowerShell` commands **must** use PowerShell 7+ (pwsh) syntax. Use `$null` instead of `/dev/null`, backtick for line continuation, `$env:VAR` for environment variables. Rely on `pre_tool_guard.py` for automatic `PYTHONIOENCODING` injection.
*   **Path Handling**: Paths **must** be double-quoted `"` and use forward slashes `/`.
*   **Environment Safety**: Rely on automated hooks (`pre_tool_guard.py`) for environment configuration (Python encoding/Conda activation, C/C++ compiler flags/sanitizer options).

### §4.3 Runtime Verification Protocol — PowerShell example (removed block)

**PowerShell (Windows)**:

```powershell
# Scenario: Verify Python behavior on Windows
# Acceptable: Isolated test using only installed libraries
PowerShell: "python -c \"import sys; print(sys.platform, sys.getdefaultencoding())\""

# Scenario: Verify struct size via C compiler on Windows
# Acceptable: Compile and run a minimal probe in temp directory
PowerShell: "$f = Join-Path $env:TEMP 'probe.c'; Set-Content $f @'`n#include <stdio.h>`nint main(void) { printf(\"int=%zu\\n\", sizeof(int)); return 0; }`n'@; gcc -o \"$env:TEMP\\probe.exe\" $f && & \"$env:TEMP\\probe.exe\""

# Unacceptable: Direct execution with potential side-effects
PowerShell: "msbuild /path/to/project.sln"                     # WRONG: Builds full project
```

### §4.4 Mandatory Skill Usage (entire section removed)

*   **Implementation Planning**: **MUST** use `deep-plan`. Enforce "Zero-Decision" and pre-flight architectural audit.
*   **Debugging & Testing**: **MUST** use `systematic-debugging`. Enforce Root Cause Analysis.
*   **TDD**: **MUST** use `test-driven-development`. No code without failing tests.
*   **Code Modification**: **MUST** use `code-modification`. Enforce downstream adaptation.
*   **Git Operations**: Follow `git-workflow`. Enforce Conventional Commits.
*   **Doc Updater**: Use `/doc-updater` to sync Core Docs (`CLAUDE.md` references) with code changes.
*   **Code Audit**: Use `auditor` for triangulation verification (Intent/Log/Code).

---

## From `style.md`

### §1.2 Communication Protocol (removed lines)

*   **Efficiency**: No pleasantries. No "I will now do X" transitions. **Directly invoke the tool.**
*   **Silent Execution (MANDATORY)**: Do NOT announce what you are going to do (e.g., "I will now edit..."). Just do it.
*   **Strict Parameter Checks**: Verify all arguments (especially `file_path`) before calling.
*   *(from Execution Strategy)*: Read-only tools may execute in parallel.
