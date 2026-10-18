---
name: remy-patch
description: Apply code changes with task-scoped strategy and recovery records. Accepts existing evidence packets or standalone requests.
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

Apply `rules/reasoning-and-decisions.md` to task strategy: distinguish requirements from evidence-supported responsibility and contract effects, then compare feasible options. Keep the existing packet fields; consume `sender_payload.analysis`, `plan[]`, and change descriptions for goals, maintained contracts, implementation bounds and verification. Optional `{修改边界：模块内｜契约要求：保持}` notation follows `rules/communication.md`, not a binary grade or an authorization mechanism.

---

## 2. Core Engineering Principles (Mandatory)

### 2.1 Data Flow & Contract Responsibility
*   **Principle**: Determine adaptation from the selected contract, actual responsibility and task boundary. Changing an upstream structure does not independently authorize migrating downstream consumers.
*   **Action**: Trace the data flow and coordinate every consumer required by the approved solution. Distinguish settled mechanical propagation from changes to semantics or ownership; do not choose scope by file count.

### 2.2 Configuration and Actual Variability
*   **Principle**: Reuse suitable constants, function arguments and configuration. Add a configuration interface when the task has an actual variability requirement, not merely because a value exists.
*   **Boundary**: Stable internal values need not become user settings. Values affecting promised behavior still require an explicit constraint or approved implementation range; report incompatibilities before proceeding.

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

### 2.6 Scoped and Modular Change
*   **Principle**: Keep the approved responsibility and contract boundaries.
    *   **Strategy**: Compare incremental and replacement options against the goal, compatibility, recovery and correction cost. A smaller diff is not sufficient if it omits required coordination.
    *   **Replacement Decision**: When an overall replacement is not already authorized, pause and explain the targets, consequences, recovery conditions and reasons before asking. Do not require proof that every incremental alternative is impossible, or re-ask an explicitly approved replacement.

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
2.  **Extract constraints**: parse `evidence_packet.proposed_changes[]` as the declared change scope and read `sender_payload.analysis`, `plan[]`, and `assumptions` for task requirements, bounded choices, strategy and verification. Check the declarations against actual applicable user approval; a packet is not permission.
    -   MUST NOT make changes outside the described and authorized scope. Evidence paths do not add targets. Missing or ambiguous target declarations require clarification, not a new packet schema.
    -   For any `evidence[]` item with `status: "suspected"`: re-read the referenced `path` and `range` and confirm before proceeding. After confirming, `Edit` the packet file to promote that item's `status` to `"confirmed"` — the PreToolUse packet check exempts that directory for remediation, but this does not independently authorize editing. Do not relabel evidence merely to pass a check. Status semantics and state machine: `~/.claude/skills/remy-plan/evidence_record.md`.
3.  Proceed to Phase 1.

**If NO argument provided:**
-   If `.claude/temp_task/.active_packet` exists, inspect its task association. Do not delete it merely because no packet argument was provided. Request confirmation before clearing state outside the approved plan; do not remove a marker to bypass an active rejection.
-   Proceed directly to Phase 1.

**Phase 1: Discovery & Tracing (Mandatory)**
1.  **Map Dependencies**:
    *   **Check**: Run `Bash("test -f .claude/logic_index.db && echo EXISTS || echo MISSING")`.
    *   **EXISTS**: Run `Bash("python \"~/.claude/skills/remy-index/impact.py\" <target_file_1> <target_file_2> ...")` with the files targeted for modification. Use the output as dependency leads and verify decision-relevant relationships against source. If exit code = 2 (no call graph data), fall through to the manual path below.
    *   **MCP alternative**: If `remy-index` MCP server is active, `query_impact` / `query_callers` tools provide equivalent data without subprocess overhead.
    *   **MISSING or exit 2**: Use `grep` or `glob` to locate all files that import or call the `target_files`.
    *   **Read**: Inspect definitions in the impact or search results that can change this implementation. Prefer needed line ranges for large files (`PRECISION_READ_THRESHOLD`, default 500); expand to surrounding or whole-file context only when needed. Record blocking unknowns. Procedure: `~/.claude/skills/remy-plan/saturation_protocol.md`.
2.  **Verify Signatures**: Read the definitions of any external functions you intend to use — NEVER infer a definition solely from its usage (principle layer: `~/.claude/skills/remy-plan/saturation_protocol.md`).

**Phase 2: Framework Compliance Check (Conditional)**

*Execute only if target files contain compiler decorators, metaprogramming patterns, or performance-critical annotations.*

1.  **Detect**: Grep target files for decorators or annotations that impose language/feature constraints (e.g., `@jit`, `@compiled`, `@cached`, framework-specific markers).
2.  **If detected**: Read the decorator/framework source or documentation to verify the new code uses only supported features.
3.  **If not detected**: Skip this phase.

**Phase 3: Execution (Read-Plan-Edit)**

For each file to be modified, execute the following sub-steps in order. Maintain a `_rollback_cache` (mapping file path → original content) for the duration of Phase 3.

### 3.1 Pre-Read & Cache

1.  **Read** the file to be edited.
2.  **Cache**: Store the full original content in `_rollback_cache[file_path]`, together with the target version/content fingerprint and this task's modifications. Preserve the material rather than assuming a later Git reset will recover it. Assign one writer per file; record the state after each owned edit so a later restore can detect concurrent changes.

### 3.2 Discovery Checkpoint (Packet Mode Only)

*Skip this sub-step entirely if no packet was loaded in Phase 0.*

**Before each `Edit` call**, evaluate the following 3 conditions:

| # | Condition | Trigger |
| :--- | :--- | :--- |
| H1 | The target or operation is outside the explicit `proposed_changes[].description` and applicable approval; evidence paths do not add modification scope | Scope overflow |
| H2 | The target function's signature or structure has changed since the audit's `evidence[].excerpt` (staleness per `~/.claude/skills/remy-plan/evidence_record.md`) | Stale plan |
| H3 | The edit would violate a constraint stated in `sender_payload.analysis` | Constraint conflict |

**If ANY condition is TRUE → Hard Interrupt:**
1.  HALT the affected edit and report the condition and evidence.
2.  Respond by cause: H1 requires an explicit scope decision; H2 requires re-establishing current facts; H3 requires resolving which requirement may change. Ask the actual user-owned question with consequences and recovery conditions. There is no generic permission to ignore stale facts or hard requirements.
3.  Continue only after the corresponding condition is resolved and the applicable plan/approval is updated. Unrelated work may continue only within its unchanged authorization.
4.  **If rollback is authorized**: Compare every target with the recorded state after this task's edits. If another writer has changed it, stop and resolve the merge instead of copying stale cached content. Restore only this task's approved modifications, using the preserved original and modification record, and report exactly what was restored or remains unresolved. Then HALT Phase 3; do not claim that all changes were rolled back if they were not.

**If ALL conditions are FALSE → Proceed to Edit.**

### 3.3 Soft Decision Log (Packet Mode Only)

*Skip if no packet was loaded in Phase 0.*

During editing, retain the logging obligation for actual behavior, resource, ordering or boundary choices NOT explicitly covered by `proposed_changes[]` or `sender_payload.analysis`. Before making a new user-owned contract, ownership, migration, external-effect or acceptance decision, obtain the required decision; an after-the-fact log is not authorization. Details within explicit implementation bounds may be chosen autonomously. Ordinary syntax choices need not be logged.

For each actual behavioral choice (e.g., an error type, default value or behaviorally relevant pattern):

1.  Record the decision in memory.
2.  After ALL edits in Phase 3 are complete, if any decisions were recorded:
    *   Ensure directory: `mkdir -p ".claude/temp_decisions"`
    *   Write to `.claude/temp_decisions/decisions_{PACKET_ID}.md`:

    ```markdown
    # Decisions Log — {PACKET_ID}

    | 文件路径 | 决策描述 | 类别 | 要求或批准依据 |
    | :--- | :--- | :--- | :--- |
    | path/to/file | 选择符合已定错误契约的异常类型 | Behavior | 实际契约或用户决定引用 |
    ```

    Categories: `Interface` / `Resource` / `Behavior` / `Ordering` / `Boundary`.
3.  If NO undocumented decisions were made, do NOT create the file.

### 3.4 Edit & Verify

1.  **Edit**: Apply the change.
2.  **Change Check**: Verify the intended edit using the tool result and an appropriate diff/static check; obey the tool contract rather than re-reading a just-edited file solely to confirm success. Record this task's resulting file state for concurrent-change detection.

### 3.5 Repeat

Repeat sub-steps 3.1–3.4 for each file in the modification set.

**Phase 4: Validation**

### 4.1 Test Selection

1.  Collect the applicable acceptance checks from `sender_payload.plan[]`, `sender_payload.analysis`, and the approved request, including stated manual, compatibility and recovery checks; do not replace them with symbol-related unit tests alone.
2.  **If impact.py was run in Phase 1**: Identify test files from the impact output (files in test directories at any depth).
3.  **If no impact data**: `Grep` for test files that import or reference the modified symbols.
4.  **If no tests or other authorized checks are available**: Skip to 4.4, retaining the outstanding acceptance items.

### 4.2 Test Execution

1.  Run the identified tests and the stated task checks within applicable authorization and mode. For manual checks, record the observed result and scope rather than inventing an exit code.
2.  Capture exit code, stdout, and stderr where applicable.
3.  Account for each planned acceptance item as executed, failed, or not executed with a reason. Preserve unknowns and permission boundaries; a passing subset does not complete omitted checks.

### 4.3 Failure Handling

If tests fail, capture the failed checks and diagnose implementation, test, or environment defects. Repair ordinary recoverable failures within the approved scope and rerun affected checks without requesting approval for each failure. Do not impose a universal two-attempt limit. Stop when attempts add no evidence, effects are unknown, or repair crosses an authorization boundary. Ask before an unapproved rollback, scope expansion, or accepting a failing check. Shared procedure: `~/.claude/skills/remy-plan/halt_protocol.md`.

### 4.4 No Tests Available

If no relevant tests exist:
1.  Print: "No tests cover the modified symbols. Consider running `/remy-inspect` for test generation and coverage analysis."
2.  Proceed with the existing no-tests report, explicitly retaining the unverified behavior and acceptance limitations. Absence of tests is not a passing result; do not silently claim an unmet acceptance requirement is complete.
