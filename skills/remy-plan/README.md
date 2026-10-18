# remy-plan (Architecture Pre-Review)

remy-plan is a **zero-code** architecture audit protocol. It forces the AI to complete thorough ambiguity elimination, invariant definition, and logic simulation before writing any implementation code. The core principle is **"Decide first, code later."**

## Core Workflow

### Phase 1: Context Saturation

The AI first checks whether `.claude/logic_index.db` exists:

- **Available**: Runs `impact.py` on the target files to produce a bidirectional impact report — upstream (who calls this code) and downstream (what this code calls), with cross-layer warnings. Definitions are read where they can change the current decision, using needed source ranges instead of a fixed all-file requirement. Cross-layer impacts are flagged for Table 3 audit.
- **Unavailable**: Prompts the user to run `/remy-index` or falls back to manual grep/glob-based exploration.

Both paths inspect definitions that affect the proposal and identify unresolved assumptions. Missing information is reported with its scope instead of claiming all dependencies are understood.

A **reuse scan** is performed: for any planned new function or utility, the project is searched for existing implementations with similar names or purposes. If a reusable function exists, the plan references it rather than proposing new code.

### Phase 2: Requirement Restatement Gate

The visible checkpoint remains. Explicit targets, verifiable acceptance and task boundaries must be complete, either in the request or an applicable approval, before skipping with `Restatement skipped: spec-complete` and its basis. An error log alone is not enough. Otherwise **Goal / expressed Motivation / Out-of-scope** is embedded in a two-option `AskUserQuestion`; do not invent a motive. Deviations are merged and the restatement loops until confirmed.

### Phase 3: Ambiguity Elimination Loop

The mandatory five-category checklist remains: Interface Contract, Resource & Dependency, Behavioral Boundary, Execution Order and Change Boundary. Each is addressed or marked N/A. User-owned alternatives are asked with a recommendation and its basis; dependent questions remain sequential. Recommendations follow goals, hard constraints, responsibility and actual decision coupling, not Implementation/Architectural grades or a default smallest diff. Optional `{field: value｜field: value}` annotations describe task attributes in the existing text fields.

After an answer, related code is searched and **cross-constraint validation** checks prior decisions. A contradiction is re-presented without silently invalidating a prior hard requirement. User decisions must be settled; implementation details may remain within explicit bounds that preserve the agreed behavior.

### Phase 4.1: Assumption Manifest & Scenario Probes

After the loop exits, factual assumptions use the shared `[✓]` / `[~]` / `[?]` / `[×]` evidence states, optionally with an acquired basis such as `[~｜source code]`. User choices use `[User decision]`, separately from evidence. `[Fact question]` may carry the current inspection/experiment method when useful; an existing method column is not duplicated. Technical questions use read-only evidence first; temporary probes require authorization and never run in native Plan mode.

Questions are batched by dependency and proposal impact, not confidence rank. Unavailable or inconclusive observations retain their evidence gap; a user's choice cannot verify a technical assertion. Refutation or rejection returns relevant premises and decisions to Phase 3. The one-pass guard limits full manifest generation, not the review of carried unresolved items. Blockers must be resolved or removed by an explicit scope decision before the plan proceeds.

### Phase 4.2: Scenario Confirmation Gate

Triggered by actual contract, responsibility, interaction or exception-behavior changes. Skip only for no such change or an approval covering the combined main and exception behavior, stating that basis. Confirm only new or changed portions not already covered. When both sections need confirmation, the AI presents one `AskUserQuestion` call with two questions; a fully approved section is cited as context rather than asked again. The sections remain distinct — the main success scenario in use-case format (Preconditions → Trigger → Main flow → Postconditions) and the extension scenarios (`condition → expected behavior`) — each carrying its narrative in the question field with Confirm/Deviation options. Wording-level deviations loop freely; a decision-level deviation re-enters the Phase 3 loop at most once, tracked by an independent `_scenario_pass` counter.

### Phase 4.3: Plan-Code Alignment Check

Before generating the tables, the AI accounts for every target's freshness: relevant code definitions, document passages or existing integration points for new files. A clearly identified still-current observation may be reused; otherwise the needed content is re-read. Contradictions return to the decision loop. This is not an optional filter over targets.

Two further pre-emit checks (Phase 4.4 Schema Deletion Tree-Wide Scan and Phase 4.5 Orphan Creation Detection) run afterwards to guard against dead code and untracked delete impact.

### Phase 5: Audit Output (5 Tables)

Once Phase 4 completes, the AI loads the template selected by the loaded `language.md` directive (`audit_template_zh.md` for Chinese or `audit_template_en.md` for English) and generates five tables:

| Table | Purpose |
| :--- | :--- |
| Ambiguity Resolution Matrix | Settles user decisions and records explicit implementation bounds; unresolved user-owned choices block handoff. |
| PBT Property Specification | Records supported properties with domains and premises; show N/A and use an empty array when none applies. Ordinary behavior checks stay in the verification table. |
| Logic & Contract Audit | Checks data-flow consistency, complexity (Big-O), concurrency risks, and system side effects. |
| Physical Change Simulation | Lists every file, function, and operation type to be modified, with ripple effect estimates. |
| Verification Plan | Maps goals, preserved contracts and selected-scenario branches to observable checks and back to their requirements, conditions and changes; includes failure handling. Reused approval does not remove coverage. |

### Phase 6: Evidence Packet Generation

After the 5 tables, when authorization, mode and absence of blockers permit it, the AI writes an `AgentTaskPacketLite` JSON file to `.claude/temp_task/task_{TIMESTAMP}.json`. The existing `analysis` and `plan[]` text fields retain acceptance methods, expected results, applicable conditions and failure handling; a required acceptance gap cannot be hidden as a nonblocking assumption. This packet contains:

- **Evidence chain**: Verbatim excerpts from every file read during the audit.
- **Proposed changes**: File-level operations mapped to evidence references.
- **Git revision**: Commit hash and timestamp for version tracking.

After an authorized packet write, `.active_packet` is updated to reference it with task-association checks; it is not deleted to bypass a rejection. The original audit and packet JSON structures remain: strategy and implementation bounds use existing text fields, not a new validator or contract extension. Native Plan writes only its designated plan file; no actual packet means no activation or execution command.

### Phase 7: Mandatory Stop

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

Without an evidence packet, `/remy-patch` still follows the user-approved scope and native permissions; packet-specific checks are unavailable and `/remy-audit` uses two-way verification. Native Plan is separate and does not generate a packet automatically. Packet presence itself is not execution authorization.

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
