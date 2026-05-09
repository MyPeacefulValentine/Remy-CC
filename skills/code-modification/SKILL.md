---
name: code-modification
description: Use this skill when modifying, refactoring, or optimizing code. Enforces strict engineering standards and project-specific constraints.
allowed-tools: Read, Edit, Write, Grep, Glob, Bash
argument-hint: "[task_packet_file (optional)]"
disable-model-invocation: true
---

# Code Modification Standards

## 1. Context Awareness Warning (Forked Agent)
**WARNING**: You are running in a **forked context** to ensure objectivity.
*   **Amnesia Risk**: You DO NOT know the full state of the project.
*   **Verification**: You MUST independently discover the call chain and dependencies. Do NOT rely on memory.

## 2. Input Requirements (Mental Model)
Although strict schema validation is disabled, you MUST internally structure your approach around these inputs:
*   **Intent**: What is the goal? (e.g., "Fix NPE in auth service")
*   **Risk Analysis**: Potential ripple effects, framework integrity (JIT/Numba), and performance risks.
*   **Target Files**: List of files targeted for modification.
*   **Call Chain Analysis**: Explicit list of upstream callers and downstream dependencies.
*   **Verification Plan**: How you will verify library signatures and framework compatibility.

---

## 3. Core Engineering Principles (Mandatory)

### 3.1 Data Flow & Hierarchy (Downstream Adapts)
*   **Principle**: Modifications should flow downstream. **Downstream consumers must adapt to upstream changes** (data structures, APIs), unless the upstream itself is buggy.
*   **Action**: Trace the data flow. If you change a core data structure, you MUST update all consumers.

### 3.2 Configuration Over Hardcoding
*   **Principle**: **Avoid hardcoding**. Use constants, config files, or function arguments.
*   **Exception**: If existing mechanisms explicitly prevent configuration (rare), you MUST inform the user before proceeding.

### 3.3 Framework Integrity (Numba, JIT, Decorators)
*   **Principle**: **Do NOT break cross-file frameworks**.
    *   **Numba/JIT**: Ensure all utilized Python features are supported by the compiler (nopython mode).
    *   **Decorators**: Respect existing metaprogramming patterns.
*   **Check**: If modifying a JIT-compiled function, verify compatibility with the specific Numba version in use.

### 3.4 Performance & Resource Management
*   **Principle**: **Do NOT degrade performance**.
    *   **Optimized Code**: Respect existing optimizations (numpy vectorization, memory views, JAX arrays).
    *   **Action**: If a change might introduce overhead (e.g., converting a view to a copy, breaking a loop fusion), you MUST justify it or find an alternative.

### 3.5 Strict Library Verification (No Assumptions)
*   **Principle**: **Verify, Don't Guess**.
    *   **Ban**: Do not assume a function exists or has a specific signature just because it "should".
    *   **Action**: You MUST read the library code (if local) or use `context7` / documentation tools to verify signatures before writing code.

### 3.6 Incremental & Modular Change
*   **Principle**: **Minimize Blast Radius**.
    *   **Strategy**: Use incremental, additive changes where possible. Avoid "Big Bang" rewrites.
    *   **Override Protocol**: If a total rewrite/override is necessary, you MUST **PAUSE** and explicitly ask for user permission, explaining why incremental change is impossible.

### 3.7 Ripple Effect & Minimalism
*   **Principle**: **Global Consistency & Minimal Noise**.
    *   **Ripple**: Analyze the call chain. Ensure logical consistency across the entire project.
    *   **Minimalism**: Do NOT touch unrelated comments, formatting, whitespace, or variable names. ONLY change what is necessary for the task.

---

## 4. Workflow Protocol

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
    *   **Read**: You MUST `Read` all files listed at Depth 0–1 in the impact output (or all grep-discovered files in the manual path).
2.  **Verify Signatures**: Read the definitions of any external functions you intend to use.

**Phase 2: Framework Compliance Check**
1.  **JIT Check**: If modifying code under `@jit` or `@numba`, verify the new logic is supported in `nopython` mode.
2.  **Array Check**: Ensure `numpy` / `jax` array operations do not trigger unintended copies or device transfers.

**Phase 3: Execution (Read-Plan-Edit)**
1.  **Pre-Read**: Read the file to be edited.
2.  **Edit**: Apply the change.
3.  **Post-Read**: Verify the change was applied correctly.

**Phase 4: Validation**
1.  Run the tests specified in your plan.
