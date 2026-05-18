---
name: deep-plan
description: Use when an implementation plan is proposed but requires a deep architectural audit for risks, side effects, and ambiguities before writing any code.
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
1.  **Check**: Run `Bash("test -f .claude/logic_index.json && echo EXISTS || echo MISSING")`.
2.  **Branch**:
    *   **EXISTS**: Proceed to **Step 1: Structured Context Saturation**.
    *   **MISSING**: Use `AskUserQuestion` to ask:
        > "`.claude/logic_index.json` does not exist. Run `/update-logic-index` to initialize? This enables automated impact analysis. Choosing No uses manual grep-based exploration instead."
        *   **User says Yes**: Invoke the `update-logic-index` skill, then proceed to **Step 1**.
        *   **User says No**: Proceed to **Step 1-Fallback: Manual Context Saturation**.

**Step 1: Structured Context Saturation (requires logic_index.json)**

*   **1a — Impact Radius Scan**: Identify the target files from the task description, then run:
    ```
    Bash("python \"~/.claude/skills/update-logic-index/impact.py\" <target_file_1> <target_file_2> ...")
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

After completing Step 1, proceed to **Step 2: Recursive Ambiguity Elimination**.

**Step 1-Fallback: Manual Context Saturation (no logic_index.json)**
Use this path when `logic_index.json` is unavailable or contains no call graph data.
1.  **Self-Correction**: Ask "Do I have the *source definition* of every dependency involved?"
2.  **Recursive Read**: If you only see usages (e.g., `db.connect()`), you MUST read the definition (e.g., `class DBConnection`).
3.  **No Hallucinations**: You are FORBIDDEN from assuming implementation details (e.g., "It likely uses requests") without evidence.
4.  **Action**: Use `Read`, `Grep`, or `Glob` to saturate your context.

**Step 2: Recursive Ambiguity Elimination (Loop-Until-Saturated)**
You MUST execute the following loop until NO ambiguities remain:

1.  **Scan**: Identify current architectural decision points based on *saturated* context.
2.  **Check**: Are there unresolved ambiguities?
    *   **NO**: Break loop and proceed to "Step 3: Finalize".
    *   **YES**: Continue to next sub-step.
3.  **Ask**: Use `AskUserQuestion` to resolve *current layer* ambiguities.
    *   **Multi-Question Batching**: Present all currently visible ambiguities.
    *   **Language**: Follow the `REMY_LANG` environment variable (`zh-CN` → Chinese, `en` → English).
    *   **Format**: Short header, reasonable options, recommended option marked.
4.  **Saturate (Again) - ACTION REQUIRED**:
    *   **Trigger**: Immediately upon receiving the user's choice.
    *   **Mandate**: You **MUST** invoke `Grep`/`Glob` targeting the specific keywords of the choice (e.g., if user selected "Redis", grep for "redis", "cache", "sentinel").
    *   **Read**: You **MUST** read any newly discovered configuration/utility files.
    *   **Blocker**: Do NOT proceed to Step 5 until these new tool outputs are visible in the context.
5.  **Repeat**: Go back to sub-step 1.

**Step 3: Finalize (Load Templates)**
Only when the loop terminates (ZERO ambiguities remain):
1.  **Action**: You **MUST** use the `Read` tool to read the template file: `skills/deep-plan/audit_template.md`.
2.  **Instruction**: Use the content of that file to structure your final report.

## 3. Analysis Output (Deferred Loading)

**CRITICAL CONSTRAINT**: You DO NOT have the output schema yet. You MUST read `skills/deep-plan/audit_template.md` in Step 3 to get it.

*   **Prohibition**: Do NOT invent your own tables.
*   **Prohibition**: Do NOT "guess" the schema.
*   **Requirement**: Use `Read` to load the schema dynamically ONLY after ambiguities are resolved.

## 4. Strict Schema Compliance (Implicit)

You MUST read `~/.claude/skills/deep-plan/output_schema.json` (if available) to understand the required verification depth.

## 5. Critical Rules
1.  **Stop & Think**: Do not generate this report if you haven't read the relevant files yet. Read them first.
2.  **Be Harsh**: The goal is to find problems, not to validate the plan. Play the "Devil's Advocate".
3.  **No Code Generation**: This step is pure analysis. Do not write implementation code here.

## 5.5 Evidence Packet Generation (Mandatory)

After generating the 4 analysis tables (Section 3), you MUST produce and write an AgentTaskPacketLite JSON file before the stop prompt. This packet is the executable contract for `/code-modification`.

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
-   In the stop prompt (Section 6), include: `📦 Packet: task_{TIMESTAMP}.json | 执行: /code-modification task_{TIMESTAMP}.json`

## 6. Explicit Stop Protocol (MANDATORY)
**CRITICAL**: You MUST generate ALL tables and analysis text in your response.

**After generating the analysis tables (loaded from template), you MUST STOP.**
1.  Do **NOT** write any code.
2.  Do **NOT** apply any changes.
3.  Do **NOT** use the `AskUserQuestion` tool.
4.  Ends your response with a clear text question to the user:
    > "审计完成 (Audit Complete). [🟢开始执行 (Proceed)] / [🟡修改计划 (Revise)] / [🔴取消 (Cancel)]?"
