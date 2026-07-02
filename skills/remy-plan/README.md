# remy-plan (Architecture Pre-Review)

remy-plan is a **zero-code** architecture audit protocol. It forces the AI to complete thorough ambiguity elimination, invariant definition, and logic simulation before writing any implementation code. The core principle is **"Decide first, code later."**

## Core Workflow

### Phase 1: Context Saturation

The AI first checks whether `.claude/logic_index.db` exists:

- **Available**: Runs `impact.py` on the target files to produce a bidirectional impact report — upstream (who calls this code) and downstream (what this code calls), with cross-layer warnings. All files at Upstream Depth 1 and Downstream Depth 1 are force-read. Cross-layer impacts are flagged for Table 3 audit.
- **Unavailable**: Prompts the user to run `/remy-index` or falls back to manual grep/glob-based exploration.

In both paths, the AI must read all relevant source code definitions. Guessing or planning based on incomplete information is prohibited.

A **reuse scan** is performed: for any planned new function or utility, the project is searched for existing implementations with similar names or purposes. If a reusable function exists, the plan references it rather than proposing new code.

### Phase 2: Ambiguity Elimination Loop

The AI scans for ambiguities using a mandatory 5-category checklist (Interface Contract, Resource & Dependency, Behavioral Boundary, Execution Order, Change Boundary). If multiple technical paths exist, the AI **must** pause and use `AskUserQuestion` to ask the user — each question must include exactly one recommended option with a 1-sentence reason. Questions with inter-dependencies are presented sequentially rather than batched.

Upon receiving an answer, the AI searches for related code and performs **cross-constraint validation** against all previously locked decisions before proceeding. If a contradiction is found, the conflicting prior decision is invalidated and re-presented. This loop repeats until all "TBD" items are converted to "Fixed" constraints.

### Phase 3.1: Assumption Manifest & Scenario Probes

After the loop exits, the AI generates a list of ALL implicit assumptions (implementation-level and behavioral-level) not already in Table 1. Each assumption includes a confidence level and category. Assumptions with confidence ≤ Level 4 trigger concrete scenario probes — the AI constructs a specific execution scenario to help the user judge whether the assumption is correct.

Assumptions are presented in batches via `AskUserQuestion`. User rejections trigger a one-time re-entry into the Phase 2 loop to resolve the new ambiguity. A second re-entry is prohibited.

### Phase 3.2: Plan-Code Alignment Check

Before generating the final tables, the AI re-reads target function signatures to confirm no concurrent external modifications invalidated the plan's assumptions. If a contradiction is detected, the ambiguity resolution loop is re-entered.

Two further pre-emit checks (Phase 3.3 Schema Deletion Tree-Wide Scan and Phase 3.4 Orphan Creation Detection) run afterwards to guard against dead code and untracked delete impact.

### Phase 4: Audit Output (5 Tables)

Once Phase 3 completes, the AI loads the language-matching template (`audit_template_zh.md` for `REMY_LANG=zh-CN`, `audit_template_en.md` for `REMY_LANG=en`) and generates five tables:

| Table | Purpose |
| :--- | :--- |
| Ambiguity Resolution Matrix | Records all decision points and their locked solutions. Any unlocked ambiguity rejects the plan. |
| PBT Property Specification | Defines mathematical invariants (idempotency, reversibility, etc.) to guide test case design. |
| Logic & Contract Audit | Checks data-flow consistency, complexity (Big-O), concurrency risks, and system side effects. |
| Physical Change Simulation | Lists every file, function, and operation type to be modified, with ripple effect estimates. |
| Verification Plan | Defines how to verify the implementation end-to-end after execution, with rollback conditions. |

### Phase 5: Evidence Packet Generation

After the 5 tables, the AI writes an `AgentTaskPacketLite` JSON file to `.claude/temp_task/task_{TIMESTAMP}.json`. This packet contains:

- **Evidence chain**: Verbatim excerpts from every file read during the audit.
- **Proposed changes**: File-level operations mapped to evidence references.
- **Git revision**: Commit hash and timestamp for version tracking.

The `.active_packet` pointer is updated to reference the new packet.

### Phase 6: Mandatory Stop

The AI stops and presents three options:

> Audit Complete. [Proceed] / [Revise] / [Cancel]?

No code is written during this phase.

## Plan-Modify-Audit Pipeline

remy-plan is the first stage of a three-skill pipeline:

```
/remy-plan
  └─→ Writes .claude/temp_task/task_{TIMESTAMP}.json
         └─→ /remy-patch task_{TIMESTAMP}.json
               (uses proposed_changes[] as authoritative constraint)
                      └─→ /remy-audit [log_file] task_{TIMESTAMP}.json
                            (three-way verification: plan vs. changelog vs. code)
```

Skipping `/remy-plan` means `/remy-patch` runs without boundary constraints and `/remy-audit` degrades to two-way verification.

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
| `audit_template_zh.md` / `audit_template_en.md` | Markdown table templates, language-selected via `REMY_LANG` (loaded dynamically during audit) |
| `output_schema.json` | JSON schema for verification depth |
| `../remy-index/impact.py` | BFS impact radius script (invoked in Phase 1.2 step (1)) |
