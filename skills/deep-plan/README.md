# Deep Plan (Architecture Pre-Review)

Deep Plan is a **zero-code** architecture audit protocol. It forces the AI to complete thorough ambiguity elimination, invariant definition, and logic simulation before writing any implementation code. The core principle is **"Decide first, code later."**

## Core Workflow

### 1. Context Saturation

The AI first checks whether `.claude/logic_index.json` exists:

- **Available**: Runs `impact.py` on the target files to produce a bidirectional impact report — upstream (who calls this code) and downstream (what this code calls), with cross-layer warnings. All files at Upstream Depth 1 and Downstream Depth 1 are force-read. Cross-layer impacts are flagged for Table 3 audit.
- **Unavailable**: Prompts the user to run `/update-logic-index` or falls back to manual grep/glob-based exploration.

In both paths, the AI must read all relevant source code definitions. Guessing or planning based on incomplete information is prohibited.

A **reuse scan** is performed: for any planned new function or utility, the project is searched for existing implementations with similar names or purposes. If a reusable function exists, the plan references it rather than proposing new code.

### 2. Interactive Ambiguity Resolution (Loop)

If multiple technical paths exist (e.g., Regex vs AST, Redis vs in-memory), the AI **must** pause and use `AskUserQuestion` (language follows `REMY_LANG`) to ask the user. Upon receiving an answer, the AI must search for related code before proceeding. This loop repeats until all "TBD" items are converted to "Fixed" constraints.

### 2.5. Plan-Code Alignment Check

Before generating the final tables, the AI re-reads target function signatures to confirm no concurrent external modifications invalidated the plan's assumptions. If a contradiction is detected, the ambiguity resolution loop is re-entered.

### 3. Strict Audit (5 Tables)

Once ambiguities are resolved, the AI loads the report template from `audit_template.md` and generates five tables:

| Table | Purpose |
| :--- | :--- |
| Ambiguity Resolution Matrix | Records all decision points and their locked solutions. Any unlocked ambiguity rejects the plan. |
| PBT Property Specification | Defines mathematical invariants (idempotency, reversibility, etc.) to guide test case design. |
| Logic & Contract Audit | Checks data-flow consistency, complexity (Big-O), concurrency risks, and system side effects. |
| Physical Change Simulation | Lists every file, function, and operation type to be modified, with ripple effect estimates. |
| Verification Plan | Defines how to verify the implementation end-to-end after execution, with rollback conditions. |

### 4. Evidence Packet Generation

After the 5 tables, the AI writes an `AgentTaskPacketLite` JSON file to `.claude/temp_task/task_{TIMESTAMP}.json`. This packet contains:

- **Evidence chain**: Verbatim excerpts from every file read during the audit.
- **Proposed changes**: File-level operations mapped to evidence references.
- **Git revision**: Commit hash and timestamp for version tracking.

The `.active_packet` pointer is updated to reference the new packet.

### 5. Mandatory Stop

The AI stops and presents three options:

> Audit Complete. [Proceed] / [Revise] / [Cancel]?

No code is written during this phase.

## Plan-Modify-Audit Pipeline

Deep Plan is the first stage of a three-skill pipeline:

```
/deep-plan
  └─→ Writes .claude/temp_task/task_{TIMESTAMP}.json
         └─→ /code-modification task_{TIMESTAMP}.json
               (uses proposed_changes[] as authoritative constraint)
                      └─→ /auditor [log_file] task_{TIMESTAMP}.json
                            (three-way verification: plan vs. changelog vs. code)
```

Skipping `/deep-plan` means `/code-modification` runs without boundary constraints and `/auditor` degrades to two-way verification.

## When to Use

- **Complex refactoring**: Modifying core logic or shared components.
- **New feature development**: Requirements are unclear or multiple implementation paths exist.
- **High-risk operations**: Data migrations, permission changes, or irreversible operations.

## Prohibitions

- **No code generation**: The AI is strictly forbidden from generating or modifying any implementation code during this phase.
- **No assumptions**: The AI must not assume user intent; confirmation via questions is required.

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Full protocol definition (loaded by Claude Code) |
| `audit_template.md` | Markdown table templates (loaded dynamically during audit) |
| `output_schema.json` | JSON schema for verification depth |
| `../update-logic-index/impact.py` | BFS impact radius script (invoked in Step 1a) |
