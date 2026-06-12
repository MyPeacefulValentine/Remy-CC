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

**Step 1: Structured Context Saturation (requires logic_index.db)**

*   **1a — Impact Radius Scan**: Identify the target files from the task description, then run:
    ```
    Bash("python \"~/.claude/skills/remy-index/impact.py\" <target_file_1> <target_file_2> ...")
    ```
    *   If exit code = 2 (no call graph data): fall through to **Step 1-Fallback**.
    *   Otherwise: record the output as the **Impact Report**.

*   **1b — Forced Read**: For every file listed at Upstream Depth 1 and Downstream Depth 1 in the Impact Report:
    *   If the Impact Report includes line ranges (e.g., `[L120-L155]`) **and** the file exceeds `PRECISION_READ_THRESHOLD` lines (default: 500), use `Read(file_path, offset=start_line, limit=end_line - start_line + 1)` for each listed function instead of reading the full file. Group adjacent functions into a single Read when their ranges overlap or are within 10 lines of each other.
    *   Otherwise, `Read` the entire file.
    *   For Depth 2+, read only files directly relevant to the planned change.
    *   **Exit Condition**: All Upstream Depth 1 and Downstream Depth 1 functions have been read. Context is saturated for the call chain dimension.

*   **1c — Cross-Layer Risk Flag**: If the Impact Report shows `⚠ Cross-layer impact`, record the affected layers. You MUST add a "Cross-layer interface compatibility" check item to Table 3 during the audit phase.

*   **1d — Supplementary Checks** (still mandatory):
    1.  **Self-Correction**: Ask "Do I have the *source definition* of every dependency involved?"
    2.  **Recursive Read**: If you only see usages (e.g., `db.connect()`), you MUST read the definition (e.g., `class DBConnection`).
    3.  **No Hallucinations**: You are FORBIDDEN from assuming implementation details without evidence.
    4.  **Reuse Scan**: For any planned new function or utility, `Grep` the project for existing implementations with similar names or purposes. If a reusable function exists, the plan MUST reference it (Modify/extend) rather than proposing a new one.

After completing Step 1, proceed to **Step 2: Recursive Ambiguity Elimination**.

**Step 1-Fallback: Manual Context Saturation (no logic_index.db)**
Use this path when `logic_index.db` is unavailable or contains no call graph data.
1.  **Self-Correction**: Ask "Do I have the *source definition* of every dependency involved?"
2.  **Recursive Read**: If you only see usages (e.g., `db.connect()`), you MUST read the definition (e.g., `class DBConnection`).
3.  **No Hallucinations**: You are FORBIDDEN from assuming implementation details (e.g., "It likely uses requests") without evidence.
4.  **Action**: Use `Read`, `Grep`, or `Glob` to saturate your context.

**Step 2: Recursive Ambiguity Elimination (Loop-Until-Saturated)**
You MUST execute the following loop until NO ambiguities remain:

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
    *   **Language**: Follow the `REMY_LANG` environment variable (`zh-CN` → Chinese, `en` → English).
    *   **Format**: Short header, reasonable options.
    *   **Recommendation (MUST)**: Every question MUST have exactly 1 recommended option. Append `（推荐）` to the recommended label and include a 1-sentence reason in its description. Omitting the recommendation is a protocol violation.
4.  **Saturate (Again) - ACTION REQUIRED**:
    *   **Trigger**: Immediately upon receiving the user's choice.
    *   **Mandate**: You **MUST** invoke `Grep`/`Glob` targeting the specific keywords of the choice (e.g., if user selected "Redis", grep for "redis", "cache", "sentinel").
    *   **Read**: You **MUST** read any newly discovered configuration/utility files.
    *   **Cross-Constraint Validation**: Compare the newly locked decision against ALL previously locked decisions. If a logical contradiction exists (e.g., "use Redis" vs. prior "no new runtime dependencies"), mark the conflicting prior decision as `invalidated` and re-present it in the next loop iteration.
    *   **Blocker**: Do NOT proceed to Step 5 until these new tool outputs are visible in the context AND no contradictions remain unresolved.
5.  **Repeat**: Go back to sub-step 1.

**Step 2.8: Assumption Manifest & Scenario Probes (Post-Loop Second Pass)**

This step targets **unknown unknowns** — assumptions Claude considers obvious but which may conflict with the user's intent. It runs ONCE after the Step 2 loop exits. A re-entry counter (`_manifest_pass`) tracks executions; if `_manifest_pass >= 1`, skip this step entirely and proceed to Step 2.9.

1.  **Generate Assumption Manifest**: List ALL implicit assumptions (implementation-level and behavioral-level) that are NOT already locked in Table 1. Each assumption MUST include:
    *   A 1-sentence statement of what is assumed.
    *   A confidence level (Level 2–5).
    *   The category it belongs to (Interface / Resource / Behavior / Ordering / Boundary).

    **MANDATORY FORMAT** — Output the manifest as a numbered list BEFORE the `AskUserQuestion` call:
    ```
    **Assumption Manifest:**
    1. [Level 3 | Behavior] Redis 连接失败时 fallback 到本地缓存而非直接抛出异常。
    2. [Level 4 | Interface] 缓存 key 使用 UTF-8 编码，不含二进制数据。
    3. [Level 5 | Resource] 生产环境 Redis 版本 ≥ 6.0。
    ```
    Each entry MUST include the `[Level N | Category]` prefix. Omitting the level number is a protocol violation.

    **Exclusion Rule**: Do NOT duplicate entries already present in Table 1 (resolved ambiguities).

2.  **Contradiction Detection**: Before presenting to the user, cross-check every assumption against every locked decision in Table 1. If a contradiction is found, flag it as a new ambiguity (do not present it as an assumption — route it directly to re-entry in sub-step 5).

3.  **Conditional Scenario Probes**: For each assumption with confidence ≤ Level 4, construct a concrete scenario that illustrates the behavioral consequence of that assumption. The scenario is embedded in the `AskUserQuestion` option description (inline format).

4.  **Present to User**: Use `AskUserQuestion` to present assumptions in batches of ≤ 4 items per call, sorted by confidence (lowest first). Each item offers:
    *   Option A: "确认该假设 (Confirm)" — assumption is correct.
    *   Option B: The scenario-driven alternative (for Level ≤ 4) or "否决 (Reject)" (for Level 5).
    *   User rejections or contradictions become new ambiguities.

5.  **Re-entry Decision**:
    *   If ALL assumptions are confirmed and no contradictions were detected: increment `_manifest_pass`, proceed to **Step 2.9**.
    *   If any assumption was rejected or a contradiction was detected: increment `_manifest_pass`, return to **Step 2** loop to resolve the new ambiguities. After this re-entry, the loop will eventually exit again; since `_manifest_pass >= 1`, Step 2.8 is skipped and flow proceeds directly to Step 2.9.

**Step 2.9: Plan-Code Alignment Check**
Before generating the final tables, verify that your plan assumptions still match the code:
1.  For each file you intend to modify (future Table 4 targets), `Read` the target function's current signature and first 5 lines of body.
2.  Confirm:
    *   Function signatures have not changed since Step 1 reads (no concurrent external modification).
    *   Constraints locked in Table 1 do not contradict the current code state.
3.  If a contradiction is found: return to **Step 2** and re-resolve the affected ambiguity.
4.  If no contradictions: proceed to **Step 3: Finalize**.

**Step 3: Finalize (Load Templates)**
Only when the loop terminates (ZERO ambiguities remain):
1.  **Action**: You **MUST** use the `Read` tool to read the template file: `skills/remy-plan/audit_template.md`.
2.  **Instruction**: Use the content of that file to structure your final report.

## 3. Analysis Output (Deferred Loading)

**CRITICAL CONSTRAINT**: You DO NOT have the output schema yet. You MUST read `skills/remy-plan/audit_template.md` in Step 3 to get it.

*   **Prohibition**: Do NOT invent your own tables.
*   **Prohibition**: Do NOT "guess" the schema.
*   **Requirement**: Use `Read` to load the schema dynamically ONLY after ambiguities are resolved.

## 4. Strict Schema Compliance (Implicit)

You MUST read `~/.claude/skills/remy-plan/output_schema.json` (if available) to understand the required verification depth.

## 5. Critical Rules
1.  **Stop & Think**: Do not generate this report if you haven't read the relevant files yet. Read them first.
2.  **Be Harsh**: The goal is to find problems, not to validate the plan. Play the "Devil's Advocate".
3.  **No Code Generation**: This step is pure analysis. Do not write implementation code here.

## 5.5 Evidence Packet Generation (Mandatory)

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
        "description": "<from Table 4 '简述' column>",
        "evidence_refs": ["E-001"]
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
