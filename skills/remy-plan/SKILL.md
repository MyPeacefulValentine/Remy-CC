---
name: remy-plan
description: Audit architecture for risks, side effects, and ambiguities before writing code. Produces evidence packets for /remy-patch. Recommended for large or complex tasks.
allowed-tools: Read, Grep, Glob, Bash, Write
argument-hint: "[plan_summary (optional)]"
disable-model-invocation: true
---

# Deep Plan Audit Protocol

This skill enforces a rigorous **Zero-Decision** pre-implementation review. It follows a strict **Decision-First** logic: resolve ambiguities -> define invariants -> audit logic -> verify physical changes.

## 1. Execution Context
**Goal**: Eliminate ALL ambiguity and architectural risk before a single line of code is written.

## 2. Context Saturation & Interactive Ambiguity Resolution (Mandatory)

**Step 0: Context Infrastructure Check**
Before saturating context, check whether structured call graph data is available.
1.  **Check**: Run `Bash("test -f .claude/logic_index.db && echo EXISTS || echo MISSING")`.
2.  **Branch**:
    *   **EXISTS**: Proceed to **Step 1: Structured Context Saturation**.
    *   **MISSING**: Use `AskUserQuestion` to ask:
        > "`.claude/logic_index.db` does not exist. Run `/remy-index` to initialize? This enables automated impact analysis. Choosing No uses manual grep-based exploration instead."
        *   **User says Yes**: Invoke the `remy-index` skill, then proceed to **Step 1**.
        *   **User says No**: Proceed to **Step 1-Fallback: Manual Context Saturation**.

**Saturation Principles** (apply to both Step 1 and Step 1-Fallback):
1.  **Self-Correction**: Ask "Do I have the *source definition* of every dependency involved?"
2.  **Recursive Read**: If you only see usages (e.g., `db.connect()`), you MUST read the definition (e.g., `class DBConnection`).
3.  **No Hallucinations**: You are FORBIDDEN from assuming implementation details without evidence.

**Step 1: Structured Context Saturation (requires logic_index.db)**

*   **1a — Impact Radius Scan**: Identify the target files from the task description, then run:
    ```
    Bash("python \"~/.claude/skills/remy-index/impact.py\" <target_file_1> <target_file_2> ...")
    *   **MCP alternative**: If `remy-index` MCP server is active, `query_impact` / `query_callers` tools provide equivalent data without subprocess overhead.
    ```
    *   If exit code = 2 (no call graph data): fall through to **Step 1-Fallback**.
    *   Otherwise: record the output as the **Impact Report**.

*   **1b — Forced Read**: For every file listed at Upstream Depth 1 and Downstream Depth 1 in the Impact Report:
    *   If the Impact Report includes line ranges (e.g., `[L120-L155]`) **and** the file exceeds `PRECISION_READ_THRESHOLD` lines (default: 500), use `Read(file_path, offset=start_line, limit=end_line - start_line + 1)` for each listed function instead of reading the full file. Group adjacent functions into a single Read when their ranges overlap or are within 10 lines of each other.
    *   Otherwise, `Read` the entire file.
    *   For Depth 2+, read only files directly relevant to the planned change.
    *   **Exit Condition**: All Upstream Depth 1 and Downstream Depth 1 functions have been read. Context is saturated for the call chain dimension.

*   **1c — Cross-Layer Risk Flag**: If the Impact Report shows `⚠ Cross-layer impact`, record the affected layers. You MUST add a "Cross-layer interface compatibility" check item to Table 3 during the audit phase.

*   **1d — Supplementary Actions** (still mandatory):
    1.  Apply all **Saturation Principles** listed above.
    2.  **Reuse Scan**: For any planned new function or utility, `Grep` the project for existing implementations with similar names or purposes. If a reusable function exists, the plan MUST reference it (Modify/extend) rather than proposing a new one.
    3.  **Runtime Probe (Optional)**: When static analysis is insufficient to verify a technical claim, execute a non-invasive probe per the constraints defined in Step 2.8 sub-step 2 (**Read-Only**, **Ephemeral** — system temp directory only, **Sandboxed** — no side-effects, no network, no package installation). Cite probe output as evidence in Table 1 or Table 3; probe results do NOT enter the Evidence Packet.

After completing Step 1, proceed to **Step 2: Recursive Ambiguity Elimination**.

**Step 1-Fallback: Manual Context Saturation (no logic_index.db)**
Use this path when `logic_index.db` is unavailable or contains no call graph data.
1.  Apply all **Saturation Principles** listed above.
2.  **Action**: Use `Read`, `Grep`, or `Glob` to saturate your context.

**Step 2: Recursive Ambiguity Elimination (Loop-Until-Saturated)**
You MUST execute the following loop until NO ambiguities remain.

**Scope Tag Reference** (used by sub-step 3 Recommendation and by Table 4):

| English | 中文 | Definition |
| :--- | :--- | :--- |
| `[Boundary-Wrap]` | `[补丁]` | Modification site is OUTSIDE the function/class where the buggy state originates (e.g., call-site guard, try/except wrapper). |
| `[Source-Modify]` | `[局部修复]` | Modification site is INSIDE the function/class where the buggy state originates. |
| `[Contract-Change]` | `[接口变更]` | Modification alters a public signature, return type, exception type, or documented invariant. |
| `[Scope-Refactor]` | `[跨域重构]` | Modification spans ≥ 2 components and re-assigns responsibility. |

When `REMY_LANG=zh-CN`, output the 中文 label; when `REMY_LANG=en`, output the English label.

1.  **Scan**: Identify current architectural decision points based on *saturated* context. You MUST explicitly check each of the following 5 categories and mark each as either "ambiguity identified" or "N/A (no decision needed)":
    *   **Interface Contract**: Function signatures, return types, error types, API shape.
    *   **Resource & Dependency**: Library choices, external services, infrastructure requirements.
    *   **Behavioral Boundary**: Timeouts, retries, failure modes, edge-case handling, empty/null inputs.
    *   **Execution Order**: Temporal dependencies, concurrency model, race conditions.
    *   **Change Boundary**: What is in-scope vs. out-of-scope for this modification.

    **MANDATORY FORMAT** — Output the scan result as a fenced block BEFORE any `AskUserQuestion` call:
    ```
    **Ambiguity Scan:**
    1. Interface Contract — ambiguity identified: <brief description>
    2. Resource & Dependency — ambiguity identified: <brief description>
    3. Behavioral Boundary — N/A (no decision needed)
    4. Execution Order — ambiguity identified: <brief description>
    5. Change Boundary — N/A (no decision needed)
    ```
    Skipping this output block is a protocol violation.
2.  **Check**: Are there unresolved ambiguities?
    *   **NO**: Break loop and proceed to "Step 2.8: Assumption Manifest & Scenario Probes".
    *   **YES**: Continue to next sub-step.
3.  **Ask**: Use `AskUserQuestion` to resolve *current layer* ambiguities.
    *   **Multi-Question Batching**: Present all currently visible ambiguities that have NO dependency between them. If question B's options depend on question A's answer, present A first; present B in the next iteration after A is resolved.
    *   **Pagination Loop (Mandatory)**: `AskUserQuestion` accepts at most 4 questions per call (`questions: maxItems=4`). If the count of independent ambiguities in this iteration exceeds 4, you MUST iterate:
        ```
        queue = independent_ambiguities   # FIFO, order from the Scan output
        while len(queue) > 0:
            batch = queue[:4]
            queue = queue[4:]
            AskUserQuestion(batch)
        ```
        Exiting sub-step 3 while `queue` is non-empty is a protocol violation.
    *   **Language**: Follow the `REMY_LANG` environment variable (`zh-CN` → Chinese, `en` → English).
    *   **Format**: Short header, reasonable options.
    *   **Recommendation (MUST)**: Every question MUST have exactly 1 recommended option. Append `（推荐）` to the recommended label and include a 1-sentence reason in its description. Omitting the recommendation is a protocol violation.
    *   **Scope Tagging (MUST)**: For each candidate option proposing a code change, prefix its description with a scope tag from the **Scope Tag Reference** table (output label per `REMY_LANG`).
    *   **Prohibited Pattern Cross-Check (MUST)**: Before marking an option as recommended, verify it does NOT trigger Prohibited Modification Patterns 1 (Symptom-Driven / Whack-a-Mole), 3 (Technical Debt / Overfitting), or 6 (Over-Engineering) from `output-styles/system-architect.md` §2.3. If `[补丁]` / `[Boundary-Wrap]` is recommended over a `[局部修复]` / `[Source-Modify]` alternative, the 1-sentence reason MUST cite one of: (a) bug source resides in third-party / read-only code, (b) the boundary IS the actual responsibility boundary, or (c) Source-Modify would trigger Prohibited Pattern 6 (Over-Engineering).
4.  **Saturate (Again) - ACTION REQUIRED**:
    *   **Trigger**: Immediately upon receiving the user's choice.
    *   **Mandate**: You **MUST** invoke `Grep`/`Glob` targeting the specific keywords of the choice (e.g., if user selected "Redis", grep for "redis", "cache", "sentinel").
    *   **Read**: You **MUST** read any newly discovered configuration/utility files.
    *   **Cross-Constraint Validation**: Compare the newly locked decision against ALL previously locked decisions. If a logical contradiction exists (e.g., "use Redis" vs. prior "no new runtime dependencies"), mark the conflicting prior decision as `invalidated` and re-present it in the next loop iteration.
    *   **Runtime Probe (Optional)**: If the chosen option involves a verifiable technical claim (e.g., "library X supports feature Y"), a runtime probe (per Step 2.8 sub-step 2 constraints) may be used here to confirm feasibility before proceeding.
    *   **Blocker**: Do NOT proceed to Step 5 until these new tool outputs are visible in the context AND no contradictions remain unresolved.
5.  **Repeat**: Go back to sub-step 1.

**Step 2.8: Assumption Manifest & Scenario Probes (Post-Loop Second Pass)**

This step targets **unknown unknowns** — assumptions Claude considers obvious but which may conflict with the user's intent. It runs ONCE after the Step 2 loop exits. A re-entry counter (`_manifest_pass`) tracks executions; if `_manifest_pass >= 1`, skip this step entirely and proceed to Step 2.9.

1.  **Generate & Triage Manifest**: List ALL implicit assumptions (implementation-level and behavioral-level) NOT already locked in Table 1. Each MUST include a 1-sentence statement, a confidence level (Level 2–5), and a category (Interface / Resource / Behavior / Ordering / Boundary).

    **MANDATORY FORMAT** — Output the manifest as a numbered list BEFORE any probe or `AskUserQuestion` call:
    ```
    **Assumption Manifest:**
    1. [Level 3 | Behavior] Redis 连接失败时 fallback 到本地缓存而非直接抛出异常。
    2. [Level 4 | Interface] 缓存 key 使用 UTF-8 编码，不含二进制数据。
    3. [Level 5 | Resource] 生产环境 Redis 版本 ≥ 6.0。
    ```
    Each entry MUST include the `[Level N | Category]` prefix. Omitting the level number is a protocol violation.

    **Contradiction Detection**: Cross-check every assumption against every locked decision in Table 1. If a contradiction is found, route it directly to sub-step 4 (re-entry) — do not present it for user confirmation.

2.  **Probe Filter (Mandatory for Probe-Feasible Assumptions)**: For each assumption with confidence ≤ Level 4, classify as **probe-feasible** or **probe-infeasible**:
    *   **Probe-feasible**: The assumption asserts a technical fact testable by a self-contained script with no project-specific runtime state, no production credentials, and no destructive side-effects. Examples: "library X version Y exposes function Z", "encoding A maps to bytes B as documented".
    *   **Probe-infeasible**: Intent / domain / business-rule assumptions. Examples: "user wants fallback over hard fail", "production peak is N RPS".

    **Probe Constraints (Read-Only, Ephemeral, Sandboxed)**: Run probes via `Bash("python -c '...'")` or compile-and-run in the system temp directory. No project side-effects, no network calls, no package installation. Cite probe output as evidence in Table 1 or Table 3 — probe results do NOT enter the Evidence Packet.

    For probe-feasible assumptions, a runtime probe is **mandatory**. If the probe confirms, elevate the assumption to Level 5 and remove it from the manifest. If the probe refutes, convert it to a new ambiguity and route to Step 2 re-entry. Probe-infeasible assumptions are retained for user confirmation in sub-step 3.

3.  **Scenario & Present (Pagination Loop — Mandatory)**: For each remaining probe-infeasible assumption with confidence ≤ Level 4, construct a concrete scenario illustrating the behavioral consequence (embed inline in the `AskUserQuestion` option description).

    Use `AskUserQuestion` to present assumptions sorted by confidence (lowest first). `AskUserQuestion` accepts at most 4 questions per call (`questions: maxItems=4`); you MUST iterate until the queue is empty:
    ```
    queue = remaining_assumptions   # sorted by confidence ascending
    while len(queue) > 0:
        batch = queue[:4]
        queue = queue[4:]
        answers = AskUserQuestion(batch)
        merge answers into state
    ```
    Exiting sub-step 3 while `queue` is non-empty is a protocol violation.

    Each item offers:
    *   Option A: "确认该假设 (Confirm)" — assumption is correct.
    *   Option B: The scenario-driven alternative (for Level ≤ 4) or "否决 (Reject)" (for Level 5).
    *   User rejections or contradictions become new ambiguities.

4.  **Re-entry Decision**:
    *   If ALL assumptions are confirmed and no contradictions were detected: increment `_manifest_pass`, proceed to **Step 2.9**.
    *   If any assumption was rejected or a contradiction was detected: increment `_manifest_pass`, return to **Step 2** loop to resolve the new ambiguities. After this re-entry, the loop will eventually exit again; since `_manifest_pass >= 1`, Step 2.8 is skipped and flow proceeds directly to Step 2.9.

**Step 2.9: Plan-Code Alignment Check**
Before emitting tables, Re-`Read` each future Table 4 target's current signature + first 5 lines. Reject if either:
(a) signature changed since Step 1 reads, or (b) current code contradicts Table 1 constraints — return to **Step 2**.
Otherwise proceed to **Step 2.9.1**.

**Step 2.9.1: Schema Deletion Tree-Wide Scan (Mandatory)**

Targets disposal-class changes: when a packet entry removes a schema column, deletes a function signature, or eliminates a named constant, single-file evidence rarely captures all parallel usages across the workspace.

1.  **Trigger**: Every planned Table 4 entry with `action == "Delete"`, OR any `Modify` entry whose summary contains the keywords `DROP COLUMN` / `删除` / `移除` / `remove` referencing an external-facing symbol.
2.  **Action**: For each removed symbol, execute the workspace-wide scan:
    ```
    Bash("grep -rn '<removed_symbol>' . --include='*.py' --include='*.json' --include='*.md'")
    ```
3.  **Categorization** of each hit:
    *   **Code hit** (`.py` / `.json` excluding docstring/comment blocks): MUST appear as a `Modify` entry in Table 4, OR be explicitly excluded in Table 1 with a rationale.
    *   **Documentation hit** (`.md`): Warning-level; record in Table 3 under `一致性 (Consistency)` as "doc string referenced" but does not block.
4.  **Rejection**: If any code hit is neither in Table 4 nor explicitly excluded → return to **Step 2** and expand the change set.
5.  After all hits are covered: proceed to **Step 2.9.2**.

**Step 2.9.2: Orphan Creation Detection (Mandatory)**

Targets newly-created executable code: a new function or module without a current-tree caller is dead code, even when its own tests pass.

1.  **Trigger**: Every Table 4 entry with `action == "Create"` whose target is executable code (function, class, module — not pure data / config / template files).
2.  **Requirement**: Each Create entry MUST declare at least one `caller_ref`:
    *   `caller_ref.caller_file` MUST be a file already present in the current tree (NOT another Create entry from this same packet).
    *   `caller_ref.evidence_ref` MUST point to an evidence entry whose excerpt shows the current code at the planned invocation site.
3.  **Action**: Verify each Create entry's `caller_ref` against the evidence list. If `caller_ref` is missing OR points only to another newly-created file (self-loop), the entry is an **orphan**.
4.  **Rejection**: Any orphan Create entry → return to **Step 2** and add the corresponding caller's `Modify` entry to scope.
5.  After all Create entries have non-orphan callers: proceed to **Step 3: Finalize**.

**Step 3: Finalize (Load Templates)**
Only when the loop terminates (ZERO ambiguities remain):
1.  **Action**: Load the template file matching `REMY_LANG`:
    *   `REMY_LANG=zh-CN` → `Read` `skills/remy-plan/audit_template_zh.md`
    *   `REMY_LANG=en` → `Read` `skills/remy-plan/audit_template_en.md`
2.  **Instruction**: Use the content of that file to structure your final report.

## 3. Analysis Output & Schema Compliance (Deferred Loading)

**CRITICAL CONSTRAINT**: You DO NOT have the output schema yet. You MUST `Read` two files in Step 3 to acquire it:
*   `skills/remy-plan/audit_template_zh.md` or `audit_template_en.md` — Markdown table templates, selected by `REMY_LANG`.
*   `~/.claude/skills/remy-plan/output_schema.json` — JSON schema defining verification depth.

*   **Prohibition**: Do NOT invent your own tables or "guess" the schema.
*   **Requirement**: Load both files dynamically ONLY after ambiguities are resolved.

## 4. Audit Ethos

**Be Harsh (Devil's Advocate)**: The goal is to find problems, not to validate the plan.
**No Code Generation**: This step is pure analysis. Do not write implementation code here.

## 5. Evidence Packet Generation (Mandatory)

After generating the 5 analysis tables (Section 3), you MUST produce and write an AgentTaskPacketLite JSON file before the stop prompt. This packet is the executable contract for `/remy-patch`.

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
    "plan": ["<step 1 from Table 4>", "<step 2>"],
    "analysis": "<key constraints and risks distilled from Table 3>",
    "assumptions": ["<any unresolved item from Table 1>"]
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
        "description": "<from Table 4 '简述' column>",
        "evidence_refs": ["E-001"]
      },
      {
        "id": "C-002",
        "action": "Create",
        "description": "<from Table 4 '简述' column>",
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

5.  **Update .active_packet** (Bash): `rm -f ".claude/temp_task/.active_packet" && echo "task_{TIMESTAMP}.json" > ".claude/temp_task/.active_packet"`

**Strict Rules:**
-   `evidence[]`: one item per file you ACTUALLY READ during this audit. Unread files MUST NOT appear.
-   `excerpt`: MANDATORY verbatim text. Summaries are prohibited.
-   `status`: use `"confirmed"` only for files read in this session; use `"suspected"` for inferred but unread files.
-   `proposed_changes[].evidence_refs`: MUST reference at least one evidence ID with `status: "confirmed"`.
-   `proposed_changes[].action`: Required; must match Table 4's `操作` column (Create / Modify / Delete).
-   `proposed_changes[].caller_refs`: Required when `action == "Create"` and the target is executable code (function, class, module — not pure data / config / template files). Each entry MUST reference an existing-tree caller via `caller_file` + `caller_function` + `evidence_ref`. See Step 2.9.2.
-   If NOT a git repo: use `"type": "filesystem"` and omit `"commit"`.
-   In the stop prompt (Section 6), include: `📦 Packet: task_{TIMESTAMP}.json | 执行: /remy-patch task_{TIMESTAMP}.json`

## 6. Explicit Stop Protocol (MANDATORY)
**CRITICAL**: You MUST generate ALL tables and analysis text in your response.

**After generating the analysis tables (loaded from template), you MUST STOP.**
1.  Do **NOT** write any code.
2.  Do **NOT** apply any changes.
3.  Do **NOT** use the `AskUserQuestion` tool.
4.  Ends your response with a clear text question to the user:
    > "审计完成 (Audit Complete). [🟢开始执行 (Proceed)] / [🟡修改计划 (Revise)] / [🔴取消 (Cancel)]?"
