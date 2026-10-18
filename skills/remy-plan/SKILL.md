---
name: remy-plan
description: Plan code changes using task-scoped strategy, five-table audits and the existing evidence-packet workflow.
allowed-tools: Read, Grep, Glob, Bash, Write
argument-hint: "[plan_summary (optional)]"
disable-model-invocation: true
---

# Deep Plan Audit Protocol

This skill preserves visible pre-implementation checks: resolve user-owned decisions -> define applicable properties -> audit logic -> verify physical changes. Follow the task-planning core in `rules/reasoning-and-decisions.md`; optional task annotations follow `rules/communication.md`. User intent, contracts, scope, and acceptance must be settled. Implementation details may remain within explicit written bounds; this is not an unresolved user decision. Ordinary conversational brevity does not cancel this skill's five-category scan or five-table audit.

## Overview

**Goal**: Resolve ambiguities that affect the proposed change and record remaining assumptions and risks. This skill produces a separate evidence packet; a request for native Plan mode does not invoke this workflow or authorize implementation.

**Flow**:

```
Phase 1 (Context Saturation)
   -> Phase 2 (Requirement Restatement Gate)
   -> Phase 3 (Ambiguity Elimination Loop)
   -> Phase 4 (Pre-Emit Verification, incl. 4.2 Scenario Confirmation Gate)
   -> Phase 5 (Analysis Output)
   -> Phase 6 (Evidence Packet)
   -> Phase 7 (Explicit Stop)
```

## Phase 1: Context Saturation

**Investigation Principles** (Phase 1.2 and 1.3): inspect definitions, interfaces, inheritance, and dependencies that can affect the current decision; do not infer implementation solely from usage. Identify unresolved assumptions instead of claiming every dependency is known. Shared procedure: `skills/remy-plan/saturation_protocol.md`.

### 1.1 Infrastructure Check

Before saturating context, check whether structured call graph data is available.

1.  **Check**: Run `Bash("test -f .claude/logic_index.db && echo EXISTS || echo MISSING")`.
2.  **Branch**:
    *   **EXISTS**: Proceed to **Phase 1.2: Structured Saturation**.
    *   **MISSING**: Use `AskUserQuestion` to ask:
        > "`.claude/logic_index.db` does not exist. Run `/remy-index` to initialize? This enables automated impact analysis. Choosing No uses manual grep-based exploration instead."
        *   **User says Yes**: Invoke the `remy-index` skill, then proceed to **Phase 1.2**.
        *   **User says No**: Proceed to **Phase 1.3: Manual Saturation Fallback**.

### 1.2 Structured Saturation (requires logic_index.db)

Execute the following four sub-tasks in order:

1.  **Impact Radius Scan**: Identify the target files from the task description, then run:
    ```
    Bash("python \"~/.claude/skills/remy-index/impact.py\" <target_file_1> <target_file_2> ...")
    *   **MCP alternative**: If `remy-index` MCP server is active, `query_impact` / `query_callers` tools provide equivalent data without subprocess overhead.
    ```
    *   If exit code = 2 (no call graph data): fall through to **Phase 1.3**.
    *   Otherwise: record the output as the **Impact Report**.

2.  **Targeted Read**: Use the Impact Report to choose definitions that can affect the planned change, at any relevant depth.
    *   Prefer listed line ranges for large files (`PRECISION_READ_THRESHOLD`, default 500); read surrounding definitions or the full file only when needed. Procedure: `skills/remy-plan/saturation_protocol.md`.
    *   **Exit Condition**: Decision-relevant interfaces and effects are understood, or remaining blocking questions are reported. An impact list is a search aid, not a requirement to read every listed function.

3.  **Cross-Layer Risk Flag**: If the Impact Report shows `⚠ Cross-layer impact`, record the affected layers. You MUST add a "Cross-layer interface compatibility" check item to Table 3 during the audit phase.

4.  **Supplementary Actions** (still mandatory):
    1.  Apply the **Investigation Principles** listed above.
    2.  **Reuse Scan**: For any planned new function or utility, `Grep` the project for existing implementations with similar names or purposes. If a reusable function exists, the plan MUST reference it (Modify/extend) rather than proposing a new one.
    3.  **Observation Task (Optional)**: When static analysis is insufficient to verify a technical claim, execute an observation task per `output-styles/system-architect.md` §4.1 (read-only inspection first; temporary experiments only within explicitly authorized scope, with no production credentials, network or package installation; native Plan mode prohibits executing probes). Cite observation output as evidence in Table 1 or Table 3; observation results do NOT enter the Evidence Packet.

After completing Phase 1.2, proceed to **Phase 2: Requirement Restatement Gate**.

### 1.3 Manual Saturation Fallback (no logic_index.db)

Use this path when `logic_index.db` is unavailable or contains no call graph data.
1.  Apply the **Investigation Principles** listed above.
2.  **Action**: Use `Read`, `Grep`, or `Glob` to saturate your context.

After completing Phase 1.3, proceed to **Phase 2: Requirement Restatement Gate**.

## Phase 2: Requirement Restatement Gate

Validates that the saturated understanding matches the user's intent BEFORE any architectural decision is made. Phase 3 scans for implementation-level decision points; it cannot detect a wrong understanding of the goal itself — this gate can.

### 2.1 Skip Predicate

Skip this gate when the task text contains BOTH:
*   (a) explicit target files or symbols, AND
*   (b) verifiable acceptance criteria.

An applicable prior approval may supply both conditions, but an error log or reproduction alone does not establish the intended result. Do not skip merely because the agent believes it understands. When skipping, emit the following declaration and name the actual request or approval covering the targets, acceptance, and boundaries:
```
Restatement skipped: spec-complete — <request/approval basis and scope>
```
Skipping without this declaration and basis is a protocol violation.

### 2.2 Restate

Compose the restatement in the language selected by the loaded `language.md` directive, structured as three sections:
*   **Goal**: what will be changed, in one or two sentences.
*   **Motivation**: the reason actually expressed by the user or a cited requirement; if none is given, say it is unspecified instead of inferring a motive.
*   **Out-of-scope**: what this task will NOT do.

### 2.3 Confirm

Use `AskUserQuestion` with the full three-section restatement embedded in the `question` field (the host does not reliably render assistant text emitted before a tool call in the same turn, so the restatement MUST live inside the question itself), with exactly two options:
*   "确认 (Confirm)" — the understanding is correct; proceed to **Phase 3**.
*   "有偏差 (Deviation)" — the user supplies corrections via free text.

On Deviation: merge the correction into the understanding, re-run any saturation reads the correction requires, then re-ask with the revised restatement embedded (2.2 → 2.3). Loop until confirmed. No re-entry counter applies here — restatement iterations are wording-level and carry no plan state.

## Phase 3: Ambiguity Elimination Loop (Loop-Until-Saturated)

Execute the following loop until ambiguities that could change this proposal are resolved or explicitly reported as blockers; record unrelated unknowns without extending the loop.

**Task Attribute Reference** (used by Phase 3.3 Recommendation and Table 4):

| Attribute | 中文 | Meaning |
| :--- | :--- | :--- |
| Modification boundary | 修改边界 | The actual objects and responsibilities allowed to change; reading consumers does not authorize modifying them. |
| Contract requirement | 契约要求 | Which consumers and observable behaviors must be preserved or may evolve, under which conditions. |
| Design concern | 设计关注 | Evidence-supported state, recovery, compatibility, or coordination obligations. |
| Decision status | 决策状态 | Whether semantics are already chosen, user decisions remain, or implementation freedom has explicit bounds. |

Use the configured language. Optional notation such as `{修改边界：模块内｜契约要求：保持｜设计关注：状态与恢复}` combines independent attributes, not mutually exclusive grades. Bind it to a task or decision and explain the factual basis and limits. Keep the existing `scope_position` string field for this description; do not change the packet structure.

**Observation Task Reference** (used by Phase 3.1 tagging and the Phase 3.3 Observation Gate):

*   **Question types** — Use `[事实问题]` / `[Fact question]` for assertions that can be checked against code, documentation, runtime, or environment evidence. Use `[用户决定]` / `[User decision]` for choices the user owns. Split mixed items into factual investigation and the dependent user decision. Use the configured language's full labels, defined in `rules/reasoning-and-decisions.md`; evidence states and acquired-basis attributes are defined separately in `style.md`.
*   **Investigation method** — Read-only inspection and experimental validation describe the current step, not different question types. Use the shared optional method attribute only when useful in a list; do not repeat it when the task table already has a method column. A temporary script is not automatically read-only or authorized. Experiments must fit the current mode, approval, and Phase 1.2 probe constraints.
*   **Ordering method**: before executing the observations of an iteration, list them in a task table `# | Target assertion | Method (read-only/experiment) | Depends on | Cost`, then schedule:
    *   **Evidence dependency** (task B interprets task A's output): topological serial order.
    *   **Fixture dependency** (tasks share a probe fixture): build the fixture once, run the group consecutively.
    *   **Adjudication dependency** (a cheap task can refute the premise of expensive ones): run the cheapest falsifier first; prune dependents whose premise is refuted.
    *   Independent low-cost direct observations run first (parallel where tools allow); experimental observations run in ascending cost order.

**Decision obligations**: Apply `rules/reasoning-and-decisions.md` rather than a binary grade. Distinguish settled mechanical propagation from choices affecting another owner's semantics, state, or feasible options. One module may need persistence/recovery analysis; several files may only require a settled rename. Unknowns retain their evidence state and affected decision; they do not default to a low-impact or minimal-change classification. Record user decisions and bounded implementation freedom in the existing `fixed_constraint` and `rationale` fields, not a new ID or status schema.

### 3.1 Scan

Identify current architectural decision points based on *saturated* context. You MUST explicitly check each of the following 5 categories and mark each as either "ambiguity identified" or "N/A (no decision needed)":
*   **Interface Contract**: Function signatures, return types, error types, API shape.
*   **Resource & Dependency**: Library choices, external services, infrastructure requirements.
*   **Behavioral Boundary**: Timeouts, retries, failure modes, edge-case handling, empty/null inputs.
*   **Execution Order**: Temporal dependencies, concurrency model, race conditions.
*   **Change Boundary**: What is in-scope vs. out-of-scope for this modification.

**MANDATORY FORMAT** — Output the scan result as a fenced block BEFORE any `AskUserQuestion` call:
```
**Ambiguity Scan:**
1. Interface Contract — ambiguity identified [用户决定]: <brief description and affected consumers>
2. Resource & Dependency — ambiguity identified [事实问题]: <brief description>
3. Behavioral Boundary — N/A (no decision needed)
4. Execution Order — [事实问题]: <fact to inspect>; then [用户决定]: <dependent choice>
5. Change Boundary — N/A (no decision needed)
```
Use the configured language's question labels in this grouped scan. Explain relevant task attributes and their evidence without assigning a grade. All five categories remain visible; N/A means genuinely inapplicable, not unexamined. A mixed issue is split into related fact and choice items, not a third question type. Facts supporting a choice carry their own shared evidence state where needed; the choice itself is not an evidence state.

### 3.2 Check

Are there unresolved ambiguities?
*   **NO**: Break loop and proceed to **Phase 4.1: Assumption Manifest & Scenario Probes**.
*   **YES**: Continue to Phase 3.3.

### 3.3 Ask

Use `AskUserQuestion` to resolve *current layer* ambiguities.
*   **Observation Gate**: Attempt the relevant permitted observations for factual items before asking. If observation is infeasible, unauthorized, mode-prohibited, or inconclusive, state the reason, inspected scope, partial evidence, and missing information. Ask only for the evidence or user decision needed to proceed; do not ask the user to confirm a technical fact as a preference. Keep unresolved facts and their dependent choices visible, and stop before emitting an executable plan if they still block the proposal. Already applicable user decisions are not asked again.
*   **Multi-Question Batching**: Present all currently visible ambiguities that have NO dependency between them. If question B's options depend on question A's answer, present A first; present B in the next iteration after A is resolved.
*   **Pagination Loop (Mandatory)**: `AskUserQuestion` accepts at most 4 questions per call (`questions: maxItems=4`). If the count of independent ambiguities in this iteration exceeds 4, you MUST iterate:
    ```
    queue = independent_ambiguities   # FIFO, order from the Scan output
    while len(queue) > 0:
        batch = queue[:4]
        queue = queue[4:]
        AskUserQuestion(batch)
    ```
    Exiting Phase 3.3 while `queue` is non-empty is a protocol violation.
*   **Language**: Follow the loaded `language.md` directive.
*   **Format**: Short header, reasonable options.
*   **Recommendation**: For a user decision with actual alternatives, include one recommended option with `（推荐）` and a one-sentence reason. A request for missing factual evidence does not require a recommended answer; do not invent facts to fill an option.
*   **Task Attributes (MUST)**: For each candidate proposing a code change, state its actual modification boundary and contract obligations using the **Task Attribute Reference**. Optional braces may summarize them; do not duplicate a table column or use an attribute as authorization.
*   **Prohibited Pattern Cross-Check (MUST)**: Before recommending an option, check symptom-only fixes, unsupported special cases, and unnecessary abstractions using `output-styles/system-architect.md` §2.3. Justify a boundary guard or internal change from the actual responsibility owner, source availability, and approved contract rather than preferring one code location by its label.
*   **Planned-Evolution Cross-Check (MUST)**: Before marking an option as recommended, check whether an authoritative written plan registers a follow-up need that the current decision touches. If it does, an option that reserves the seam the plan names (interface, parameter, data shape) MAY be recommended over the minimal-change alternative, and its 1-sentence reason MUST cite the plan anchor. Futures not registered in a plan remain governed by YAGNI (see the approved-follow-up interface guidance in `output-styles/system-architect.md` §2.2).
*   **Task-Driven Recommendation (MUST)**: Exclude options that violate the goal, hard constraints, or necessary responsibilities before comparing compatibility, correction cost, complexity, failure behavior, and change volume where relevant. Do not default to the smallest diff or to redesign. Analyze state transitions, owners, concurrency, persistence and recovery when affected behavior requires it, even inside one module; do not invent alternatives or state machines for settled mechanical work. Unknowns affecting the recommendation require observation or a conditional proposal, not a default downgrade. Record the strategy and bounded implementation freedom in Table 1 and the existing handoff text fields. The Planned-Evolution Cross-Check still applies, but coordination needed by the current goal requires no separate future plan.

### 3.4 Saturate (Again) — ACTION REQUIRED

*   **Trigger**: Immediately upon receiving the user's choice.
*   **Mandate**: You **MUST** invoke `Grep`/`Glob` targeting the specific keywords of the choice (e.g., if user selected "Redis", grep for "redis", "cache", "sentinel").
*   **Read**: Inspect newly discovered definitions or configuration only when they can change the selected option; record other unknowns without expanding scope.
*   **Cross-Constraint Validation**: Compare the newly locked decision against ALL previously locked decisions. If a logical contradiction exists (e.g., "use Redis" vs. prior "no new runtime dependencies"), record both requirements and re-present the conflict in the next loop iteration. A newer choice does not by itself invalidate a prior hard constraint; resolve which requirement the user intends to revise before continuing.
*   **Observation Task (Optional)**: If the chosen option involves a verifiable technical claim (e.g., "library X supports feature Y"), an observation task (per `output-styles/system-architect.md` §4.1) may be used here to confirm feasibility before proceeding.
*   **Blocker**: Do NOT proceed to **Phase 3.5** until these new tool outputs are visible in the context AND no contradictions remain unresolved.

### 3.5 Repeat

Go back to Phase 3.1.

## Phase 4: Pre-Emit Verification

Five sub-steps executed in order after the Phase 3 loop exits. Any sub-step may return the flow to Phase 3 to resolve newly discovered ambiguities.

### 4.1 Assumption Manifest & Scenario Probes (Post-Loop Second Pass)

This step checks implicit assumptions that could change the proposal. `_manifest_pass` limits full manifest generation to one pass. On re-entry, do not generate the full manifest again, but always recheck carried unresolved facts, refuted premises, and dependent decisions before proceeding to Phase 4.2. The counter never bypasses an unresolved blocker.

1.  **Generate & Triage Manifest**: List decision-relevant assumptions not already locked in Table 1, with an Interface / Resource / Behavior / Ordering / Boundary category. Factual assertions use the shared evidence state from `style.md`, with an acquired-basis attribute only when useful. User choices use the configured language's user-decision label, not an evidence state. Split mixed entries before routing.

    Output the numbered manifest before new observations or questions. These examples illustrate format only; use actual citations rather than treating placeholders as evidence:
    ```
    **Assumption Manifest:**
    1. [用户决定] Behavior: Redis 连接失败时使用本地缓存，还是终止操作？
    2. [~｜源码] Interface: 已检查路径使用 UTF-8；其他输入路径尚未核对。依据：<file:line>。
    3. [✓｜规范] Resource: 已读取的部署清单指定 Redis 6.x。依据：<file:line and revision>；不代表生产实际版本已验证。
    ```
    Use equivalent full question labels and basis words for the configured language. Retain the underlying claim and scope when updating its state. Cross-check the manifest against locked decisions; explicit counterevidence uses `[×]`, while an unresolved evidence conflict uses `[?]` with the conflicting observations stated.

2.  **Observation Filter**: For unresolved factual assertions marked `[?]` or `[~]`, identify an allowed observation that could resolve them. Actual production peak load is a factual question; choosing the target capacity or fallback behavior is a user decision.

    **Probe Constraints**: Read-only inspection comes first. Temporary experiments require explicit applicable authorization and an execution-permitting mode; no production credentials, network, package installation, or destructive effects. Native Plan mode prohibits executing probes. A probe must be self-contained and not depend on unavailable project-specific runtime state. Cite observation output in Table 1 or Table 3; probe results do NOT enter the Evidence Packet.

    - If evidence establishes the assertion, mark it `[✓]` only within its checked scope. Preserve the citation and remove it from the unresolved queue, not from the evidence record. Source inspection does not verify runtime behavior.
    - If counterevidence refutes it, mark it `[×]`, retain the examined scope, and route changed premises and dependent decisions to step 4.
    - If observation is unavailable, unauthorized, mode-prohibited, or inconclusive, retain `[?]` or the supported `[~]`. Record why, partial results, missing evidence, and whether the gap blocks this proposal. Route it to step 3 rather than dropping it.
    - Route user choices to step 3 without trying to prove a preference.

3.  **Scenario & Present**: Present remaining user decisions with concrete behavioral scenarios. Offer the proposed behavior and a meaningful alternative; do not re-ask choices already covered by applicable approval. For factual gaps, ask only for missing evidence or the authorization/scope decision needed to proceed, not for confirmation that an unverified assertion is true. Accepting a planning assumption does not change its evidence state.

    Ask dependencies before dependents, and preserve manifest order among independent items; do not sort evidence states as confidence scores. Use at most 4 independent questions per `AskUserQuestion` call, merge answers, and recompute the remaining queue before the next batch. Do not exit while an actionable question remains queued.

4.  **Re-entry Decision**:
    - On rejection, refutation, or conflict with a locked decision, increment `_manifest_pass` and return to Phase 3 carrying unresolved facts and dependent choices.
    - Proceed only when user choices are resolved and no factual gap blocks the proposal. An explicit scope revision may remove a dependency on an unknown fact; preserve the unknown and explain the revised boundary.
    - If a blocker cannot be resolved within current access, authorization, or mode, stop and report it rather than claiming confirmation.
    - On completion, increment `_manifest_pass` and proceed to Phase 4.2. Re-entry skips only full manifest generation; unresolved-item review remains required.

### 4.2 Scenario Confirmation Gate

Synthesizes the locked decisions into an end-to-end behavioral narrative for user validation. Individual decision confirmations (Phase 3, Phase 4.1) do not equal confirmation of their combined behavior — this gate closes that gap.

Carry the goal, preserved contracts and the selected approach's applicable main, alternative and exception conditions into the scenarios. Applicable conditions include branches that may occur, not only paths already observed. Reusing approval or skipping repeated confirmation does not remove these obligations or their acceptance checks; retain the covering scenario and its basis. Unselected alternatives do not require implementation or acceptance, but rejecting an alternative does not remove an otherwise applicable preservation requirement. Form the scenario content here without loading the Phase 5 template or schema early.

1.  **Skip Predicate**: Evaluate actual changes to contracts, responsibilities, user-visible behavior and exception behavior, not a scope label. Skip only when there is no such change, or an applicable approval already covers the complete main and exception scenarios. State that basis and its scope when skipping; do not assume individually approved choices cover their combined behavior. Emit exactly one declaration before entering Phase 4.3:
    ```
    Scenario confirmation skipped: <no interface/behavior change, or covering approval and scope>
    ```
    Skipping without this declaration is a protocol violation.

2.  **Compose Scenario**, in the language selected by the loaded `language.md` directive:
    *   **Main success scenario** (use-case format): Preconditions → Trigger → Main flow (numbered steps) → Postconditions.
    *   **Extensions**: each alternative/exception scenario listed as one `condition → expected behavior` item.

3.  **Confirm**: Use ONE `AskUserQuestion` call with TWO questions when both sections contain uncovered changes — question 1 carries the full main success scenario in its `question` field, question 2 carries the full extensions list in its `question` field (the host does not reliably render assistant text emitted before a tool call in the same turn, so the narrative MUST live inside the questions). Identify already approved parts as context and ask only to confirm the new or changed, uncovered portions; keep main and exception confirmation distinct. If one entire section is already covered, cite its approval and include it as context in the remaining question instead of asking it again. Each question offers exactly two options:
    *   "确认 (Confirm)" — the narrative matches the intent; when BOTH sections are covered by applicable approval or fresh confirmation, proceed to **Phase 4.3**.
    *   "有偏差 (Deviation)" — the user supplies corrections via free text; a Deviation answer on either question enters step 4 routing.

4.  **Deviation Routing**: Classify the correction:
    *   **Wording-level** (the narrative misdescribes an already-locked decision): fix the narrative, recompose (step 2), and re-ask (step 3). Loops freely.
    *   **Decision-level** (a locked decision itself is wrong): governed by an independent counter `_scenario_pass` (initial 0; do NOT reuse `_manifest_pass`). If `_scenario_pass == 0`: set it to 1, convert the correction into a new ambiguity, and return to the **Phase 3** loop. After that loop exits, Phase 4.1 skips only full manifest generation (`_manifest_pass >= 1`); carried unresolved items must be reviewed and blockers resolved or removed by an explicit scope decision before this gate is re-presented. If `_scenario_pass >= 1`: a second decision-level re-entry is prohibited — apply a wording-level correction if possible, otherwise HALT and report the unresolvable conflict.

### 4.3 Plan-Code Alignment Check

Before emitting tables, account for the freshness of every future Table 4 target. Check the relevant current definition and behavior for code, the target passage for documents, and an existing integration point for Create entries. Reuse a clearly identified fresh observation only while it still covers the current target; otherwise Re-`Read` the needed range. Do not treat the first five lines as evidence for the rest of a function. Reject if either:
(a) a relevant definition or behavior changed since the planning evidence, or (b) current content contradicts Table 1 constraints — return to **Phase 3**.
Otherwise proceed to **Phase 4.4**. Every target must have a freshness account; this is not an optional relevance filter over the target list.

### 4.4 Schema Deletion Tree-Wide Scan (Mandatory)

Targets disposal-class changes: when a packet entry removes a schema column, deletes a function signature, or eliminates a named constant, single-file evidence rarely captures all parallel usages across the workspace.

1.  **Trigger**: Every planned Table 4 entry with `action == "Delete"`, OR any `Modify` entry whose summary contains the keywords `DROP COLUMN` / `删除` / `移除` / `remove` referencing an external-facing symbol.
2.  **Action**: For each removed symbol, execute the workspace-wide scan:
    ```
    Bash("grep -rn '<removed_symbol>' . --include='*.py' --include='*.json' --include='*.md'")
    ```
3.  **Categorization** of each hit:
    *   **Code hit** (`.py` / `.json` excluding docstring/comment blocks): MUST appear as a `Modify` entry in Table 4, OR be explicitly excluded in Table 1 with a rationale.
    *   **Documentation hit** (`.md`): Warning-level; record in Table 3 under `一致性 (Consistency)` as "doc string referenced" but does not block.
4.  **Rejection**: If any code hit is neither in Table 4 nor explicitly excluded → return to **Phase 3** and expand the change set.
5.  After all hits are covered: proceed to **Phase 4.5**.

### 4.5 Orphan Creation Detection (Mandatory)

Targets newly-created executable code: a new function or module without a current-tree caller is dead code, even when its own tests pass.

1.  **Trigger**: Every Table 4 entry with `action == "Create"` whose target is executable code (function, class, module — not pure data / config / template files).
2.  **Requirement**: Each Create entry MUST declare at least one `caller_ref`:
    *   `caller_ref.caller_file` MUST be a file already present in the current tree (NOT another Create entry from this same packet).
    *   `caller_ref.evidence_ref` MUST point to an evidence entry whose excerpt shows the current code at the planned invocation site.
3.  **Action**: Verify each Create entry's `caller_ref` against the evidence list. If `caller_ref` is missing OR points only to another newly-created file (self-loop), the entry is an **orphan**.
4.  **Rejection**: Any orphan Create entry → return to **Phase 3** and add the corresponding caller's `Modify` entry to scope.
5.  After all Create entries have non-orphan callers: proceed to **Phase 5: Analysis Output**.

## Phase 5: Analysis Output

**Audit Ethos**:
*   **Be Harsh (Devil's Advocate)**: The goal is to find problems, not to validate the plan.
*   **No Code Generation**: This step is pure analysis. Do not write implementation code here.

**CRITICAL CONSTRAINT**: You DO NOT have the output schema until Phase 5.1. Load it there.
*   **Prohibition**: Do NOT invent your own tables or "guess" the schema.
*   **Requirement**: Load both template and schema dynamically ONLY after Phase 4 exits.

### 5.1 Load Template

When Phase 4 completes with decision-relevant ambiguities resolved and pre-emit checks satisfied, record any remaining nonblocking uncertainty:

1.  **Action**: Load the template file matching `REMY_LANG`:
    *   `REMY_LANG=zh-CN` → `Read` `skills/remy-plan/audit_template_zh.md`
    *   `REMY_LANG=en` → `Read` `skills/remy-plan/audit_template_en.md`
2.  **Also Load**: `~/.claude/skills/remy-plan/output_schema.json` — JSON schema defining verification depth.
3.  **Instruction**: Use the content of these files to structure your final report.

### 5.2 Emit Analysis Tables

Emit the 5 Markdown tables per the loaded template, in the exact order specified. Each table MUST include 1 empty line before and after. Follow all Strict Rules stated in the template header.

Before completing the tables, check acceptance coverage in both directions:
- Map each goal, preservation requirement and applicable branch of the selected approach to a Table 5 check with a method and observable expected result. Reverse-check each check against its requirement, basis, applicable conditions and related Table 4 changes. Checking an unchanged consumer does not authorize modifying it.
- One check may cover multiple obligations if their conditions and results remain distinguishable; use existing text fields, not an additional table or ID system. Do not require checks for unselected alternatives or demand support for version/data combinations outside the agreed contract; retain checks for required rejection or isolation of such combinations.
- N/A means genuinely inapplicable with a reason, not unknown, unexamined or not yet executed. A check planned for execution after implementation is not a blocker merely because it has not run, nor is it a passing result. Unknowns that prevent defining acceptance or establishing proposal readiness remain blockers for the affected scope.
- Fill a missing mapping directly when the underlying requirements and decisions are settled. If it exposes a new fact or user decision, use the existing routing, halt rules and budgets; do not add a confirmation loop. Excluding or deferring a required check needs an applicable user decision when it changes the acceptance scope.

## Phase 6: Evidence Packet Generation (Mandatory)

After generating the 5 analysis tables (Phase 5.2), produce and write an AgentTaskPacketLite JSON file before the stop prompt only when current authorization and mode permit it and no decision-relevant blocker remains. Otherwise stop with the plan and its limitations; do not write the packet or active pointer, and do not advertise an execution command for a nonexistent packet. This packet is the executable contract for `/remy-patch`.

Preserve the Table 5 acceptance methods, expected results, applicable conditions and failure handling in `sender_payload.plan[]`, with their requirements and selected-approach boundaries in `analysis`. Summarizing must not drop an applicable obligation or its coverage. References are sufficient only if the corresponding check content remains available in the handoff; a bare table reference is not a substitute for omitted content. Do not move an acceptance blocker into nonblocking `assumptions` or describe a planned check as already passed.

**Steps (execute in order):**

1.  **Get timestamp** (Bash): `date +"%Y%m%d_%H%M%S"` → use result as `{TIMESTAMP}`
2.  **Get git commit** (Bash, if git repo): `git rev-parse HEAD` → use result as `{COMMIT}`
3.  **Ensure directory** (Bash): `mkdir -p ".claude/temp_task"`
4.  **Write packet** (Write tool) to `.claude/temp_task/task_{TIMESTAMP}.json`:

```json
{
  "v": "1.0.0",
  "task": {
    "id": "task_{TIMESTAMP}",
    "mode": "write",
    "summary": "<one sentence describing the change scope from Table 4>",
    "read_only_until_evidence": true
  },
  "sender_payload": {
    "plan": ["<step 1 from Table 4, dependencies and related Table 5 acceptance methods, expected results, applicable conditions and failure handling>", "<step 2 with its related acceptance checks>"],
    "analysis": "<goal, hard constraints, preserved contracts, task attributes, strategy, user decisions and bounded implementation freedom from Tables 1/3/4/5>",
    "assumptions": ["<remaining nonblocking uncertainty, affected scope and required follow-up>"]
  },
  "evidence_packet": {
    "source_revision": {
      "type": "git",
      "commit": "{COMMIT}",
      "retrieved_at": "<current ISO-8601 datetime>"
    },
    "evidence": [
      {
        "id": "E-001",
        "file_type": "source",
        "path": "<repo-relative path>",
        "range": {"start": 1, "end": 50},
        "why": "<why this file/range is relevant to the planned change>",
        "status": "confirmed",
        "confidence": 0.9,
        "excerpt": "<verbatim text from that range — do NOT summarize>"
      }
    ],
    "proposed_changes": [
      {
        "id": "C-001",
        "action": "Modify",
        "description": "<Table 4 file path, location, action, summary, preserved behavior and applicable boundary>",
        "evidence_refs": ["E-001"]
      },
      {
        "id": "C-002",
        "action": "Create",
        "description": "<Table 4 file path, location, action, summary, preserved behavior and applicable boundary>",
        "evidence_refs": ["E-002"],
        "caller_refs": [
          {
            "caller_file": "<repo-relative path to an existing-tree file that will invoke this new symbol>",
            "caller_function": "<function name in caller_file>",
            "evidence_ref": "E-003"
          }
        ]
      }
    ]
  }
}
```

5.  **Update .active_packet**: After the packet was actually written, inspect any existing marker and its task association. Within applicable authorization, use the file-writing tool to set `.claude/temp_task/.active_packet` to `task_{TIMESTAMP}.json`. Preserve this producer activation step; do not delete a marker to bypass an existing rejection or activate a different task without resolving the conflict.

**Strict Rules:**
-   **Evidence Record (MUST)**: evidence[] lists only files ACTUALLY READ; excerpt is verbatim (no summaries); `confirmed` = read this session, `suspected` = inferred unread; every change's evidence_refs points to ≥1 `confirmed` entry; non-git repos use `"type": "filesystem"` without `"commit"`. Full definition: `skills/remy-plan/evidence_record.md`.
-   `proposed_changes[].action`: Required; must match Table 4's `操作` column (Create / Modify / Delete).
-   `proposed_changes[].caller_refs`: Required when `action == "Create"` and the target is executable code (function, class, module — not pure data / config / template files). Each entry MUST reference an existing-tree caller via `caller_file` + `caller_function` + `evidence_ref`. See Phase 4.5.
-   If the packet was written, include in the stop prompt (Phase 7): `📦 Packet: task_{TIMESTAMP}.json | 执行: /remy-patch task_{TIMESTAMP}.json`. Otherwise report why no packet was created, without an execution command.

## Phase 7: Explicit Stop Protocol (MANDATORY)

**CRITICAL**: You MUST generate ALL tables and analysis text in your response.

**After generating the analysis tables (loaded from template), you MUST STOP.**
1.  Do **NOT** write any code.
2.  Do **NOT** apply any changes.
3.  Do **NOT** use the `AskUserQuestion` tool.
4.  Ends your response with a clear text question to the user:
    > "审计完成 (Audit Complete). [🟢开始执行 (Proceed)] / [🟡修改计划 (Revise)] / [🔴取消 (Cancel)]?"
