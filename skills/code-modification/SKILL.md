---
name: code-modification
description: Use this skill when modifying, refactoring, or optimizing code. Enforces strict engineering standards and project-specific constraints.
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, AskUserQuestion
argument-hint: "[task_packet_file (optional)]"
disable-model-invocation: true
---

# Code Modification Standards

## 1. Input Requirements (Mental Model)
Although strict schema validation is disabled, you MUST internally structure your approach around these inputs:
*   **Intent**: What is the goal? (e.g., "Fix NPE in auth service")
*   **Risk Analysis**: Potential ripple effects, framework integrity, and performance risks.
*   **Target Files**: List of files targeted for modification.
*   **Call Chain Analysis**: Explicit list of upstream callers and downstream dependencies.
*   **Verification Plan**: How you will verify library signatures and framework compatibility.

---

## 2. Core Engineering Principles (Mandatory)

### 2.1 Data Flow & Hierarchy (Downstream Adapts)
*   **Principle**: Modifications should flow downstream. **Downstream consumers must adapt to upstream changes** (data structures, APIs), unless the upstream itself is buggy.
*   **Action**: Trace the data flow. If you change a core data structure, you MUST update all consumers.

### 2.2 Configuration Over Hardcoding
*   **Principle**: **Avoid hardcoding**. Use constants, config files, or function arguments.
*   **Exception**: If existing mechanisms explicitly prevent configuration (rare), you MUST inform the user before proceeding.

### 2.3 Framework & Compiler Integrity
*   **Principle**: **Do NOT break cross-file frameworks or compiler constraints**.
*   **Check**: If the target file uses compiler decorators (e.g., JIT compilation, AOT compilation), metaprogramming patterns, or code generation — verify that new code is compatible with those constraints.
*   **Action**: Read the decorator/framework documentation or source to confirm supported language features before modifying decorated functions.

### 2.4 Performance & Resource Management
*   **Principle**: **Do NOT degrade performance on critical paths**.
*   **Check**: If the target code is on a performance-critical path (loop bodies, hot functions, data pipelines), verify that the change does not introduce unnecessary copies, allocations, or serialization overhead.
*   **Action**: If a change might introduce overhead, you MUST justify it or find an alternative.

### 2.5 Strict Library Verification (No Assumptions)
*   **Principle**: **Verify, Don't Guess**.
    *   **Ban**: Do not assume a function exists or has a specific signature just because it "should".
    *   **Action**: You MUST read the library code (if local) or use `context7` / documentation tools to verify signatures before writing code.

### 2.6 Incremental & Modular Change
*   **Principle**: **Minimize Blast Radius**.
    *   **Strategy**: Use incremental, additive changes where possible. Avoid "Big Bang" rewrites.
    *   **Override Protocol**: If a total rewrite/override is necessary, you MUST **PAUSE** and explicitly ask for user permission, explaining why incremental change is impossible.

### 2.7 Ripple Effect & Minimalism
*   **Principle**: **Global Consistency & Minimal Noise**.
    *   **Ripple**: Analyze the call chain. Ensure logical consistency across the entire project.
    *   **Minimalism**: Do NOT touch unrelated comments, formatting, whitespace, or variable names. ONLY change what is necessary for the task.

---

## 3. Workflow Protocol

**Phase 0: Packet Loading (Conditional)**

*Execute only if a `task_packet_file` argument was provided.*

**If argument IS provided:**
1.  **Read**: Load `.claude/temp_task/{task_packet_file}` using the `Read` tool.
    -   If file does not exist: HALT. Report error. Do NOT proceed.
2.  **Extract constraints**: parse `evidence_packet.proposed_changes[]` as the authoritative change scope.
    -   MUST NOT make changes outside the described scope.
    -   For any `evidence[]` item with `status: "suspected"`: re-read the referenced `path` and `range` and confirm before proceeding.
3.  Proceed to Phase 1.

**If NO argument provided:**
-   If `.claude/temp_task/.active_packet` exists: run `Bash("rm -f '.claude/temp_task/.active_packet'")` to clear stale state.
-   Proceed directly to Phase 1.

**Phase 1: Discovery & Tracing (Mandatory)**
1.  **Map Dependencies**:
    *   **Check**: Run `Bash("test -f .claude/logic_index.json && echo EXISTS || echo MISSING")`.
    *   **EXISTS**: Run `Bash("python \"~/.claude/skills/update-logic-index/impact.py\" <target_file_1> <target_file_2> ...")` with the files targeted for modification. Use the output as the definitive dependency list. If exit code = 2 (no call graph data), fall through to the manual path below.
    *   **MISSING or exit 2**: Use `grep` or `glob` to locate all files that import or call the `target_files`.
    *   **Read**: For every file at Upstream Depth 1 and Downstream Depth 1 in the impact output (or all grep-discovered files in the manual path):
        *   If the output includes line ranges (e.g., `[L120-L155]`) **and** the file exceeds `PRECISION_READ_THRESHOLD` lines (default: 500), use `Read(file_path, offset=start_line, limit=end_line - start_line + 1)` for each listed function. Group adjacent functions into a single Read when their ranges overlap or are within 10 lines.
        *   Otherwise, `Read` the entire file.
2.  **Verify Signatures**: Read the definitions of any external functions you intend to use.

**Phase 2: Framework Compliance Check (Conditional)**

*Execute only if target files contain compiler decorators, metaprogramming patterns, or performance-critical annotations.*

1.  **Detect**: Grep target files for decorators or annotations that impose language/feature constraints (e.g., `@jit`, `@compiled`, `@cached`, framework-specific markers).
2.  **If detected**: Read the decorator/framework source or documentation to verify the new code uses only supported features.
3.  **If not detected**: Skip this phase.

**Phase 3: Execution (Read-Plan-Edit)**
1.  **Pre-Read**: Read the file to be edited.
2.  **Edit**: Apply the change.
3.  **Post-Read**: Verify the change was applied correctly.

**Phase 4: Validation**

### 4.1 Test Selection

1.  **If impact.py was run in Phase 1**: Identify test files from the impact output (files in test directories at any depth).
2.  **If no impact data**: `Grep` for test files that import or reference the modified symbols.
3.  **If no tests found**: Skip to 4.4.

### 4.2 Test Execution

1.  Run the identified tests using the project's test framework.
2.  Capture exit code, stdout, and stderr.

### 4.3 Failure Handling

If tests fail:
1.  **Report**: Print the failing test names, error messages, and triage conclusion (test defect vs. implementation defect).
2.  **Ask**: Use `AskUserQuestion` to present options:
    -   "Apply fix to implementation" — attempt to fix the regression.
    -   "Revert changes" — undo the edits made in Phase 3 (use `Edit` to restore original content).
    -   "Ignore and continue" — accept the failure (user takes responsibility).
3.  **If user chooses fix**: Apply fix, re-run tests. If still failing after 2 attempts, HALT and report.
4.  **If user chooses revert**: Restore original file content from Pre-Read state.

### 4.4 No Tests Available

If no relevant tests exist:
1.  Print: "No tests cover the modified symbols. Consider running `/post-verify` for test generation and coverage analysis."
2.  Proceed without validation.
