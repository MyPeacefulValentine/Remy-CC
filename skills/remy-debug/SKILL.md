---
name: remy-debug
description: Diagnose bugs via hypothesis loop with circuit breaker. Diagnosis only — produces evidence packets for /remy-patch without modifying code.
allowed-tools: Read, Grep, Glob, Bash, PowerShell, Write, AskUserQuestion
argument-hint: "[error_description | test_command | file:line] [--since <ref>]"
disable-model-invocation: true
---

# Remy Debug Protocol

Diagnosis-only debugging skill. Produces a structured diagnosis report and an evidence packet compatible with `/remy-patch`. Does NOT modify source code.

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `DEBUG_MAX_HYPOTHESES` | `3` | Maximum hypothesis iterations before circuit breaker triggers. |

### Argument Parsing

```
/remy-debug [symptom] [--since <git-ref>]
```

1. If no argument provided: enter **Guided Mode** (Phase 0.1).
2. If argument contains a test file path pattern (e.g., `test_*.py`, `*.test.ts`, `*_test.go`): treat as **test command**.
3. If argument matches `<file>:<line>` pattern: treat as **location reference**.
4. Otherwise: treat as **error description**.
5. `--since <ref>`: optional git revision to scope regression analysis.

---

## Phase 0: Symptom Capture

**Goal**: Obtain a concrete, actionable symptom description.

### 0.1 Guided Mode (no arguments)

Use `AskUserQuestion` to collect:
- What is the observed behavior?
- What is the expected behavior?
- When did it start (if known)?

### 0.2 Test Command Mode

1. Execute the test command via `Bash` (or `PowerShell` on Windows).
2. Capture exit code, stdout, stderr.
3. If exit code = 0: report "Test passes. No failure to diagnose." → HALT.
4. Record full error output as the **Symptom Record**.

### 0.3 Error Description Mode

Record the user-provided text as the **Symptom Record**.

### 0.4 Location Reference Mode

1. `Read` the specified file at the given line (±20 lines context).
2. Record the code region as the **Symptom Record**.

**Phase 0 Exit**: A Symptom Record exists. Proceed to Phase 1.

---

## Phase 1: Localization

**Goal**: Identify suspect files and narrow the search space.

### 1.1 Stack Trace Parsing

If the Symptom Record contains a stack trace or error with file paths:
1. Extract all file paths and line numbers mentioned.
2. These become the **Suspect Set**.

If no parseable paths exist:
1. `Grep` for keywords from the error message (function names, error codes, unique strings).
2. Results become the **Suspect Set**.

### 1.2 Impact Analysis (Conditional)

1. Check: `Bash("test -f .claude/logic_index.db && echo EXISTS || echo MISSING")`.
2. **EXISTS**: Run `Bash("python \"~/.claude/skills/remy-index/impact.py\" <suspect_file_1> <suspect_file_2> ...")`.
    *   **MCP alternative**: If `remy-index` MCP server is active, `query_impact` / `query_callers` tools provide equivalent data without subprocess overhead.
   - If exit code = 0: record output as **Dependency Map**.
   - If exit code = 2: skip (no call graph data).
3. **MISSING**: Skip impact analysis.

### 1.3 Git History Analysis

1. For each file in the Suspect Set:
   ```
   git log -5 --oneline -- <file>
   ```
2. If `--since <ref>` was provided:
   ```
   git diff <ref>..HEAD -- <suspect_files>
   ```
3. Record recent changes as **Change History**.

### 1.4 Context Read

For each file in the Suspect Set:
- If Dependency Map provides line ranges and file exceeds 500 lines: precision-read relevant ranges.
- Otherwise: read the full file.

**Phase 1 Exit**: Suspect Set, Dependency Map (if available), Change History, and suspect code are in context.

---

## Phase 2: Hypothesis Loop

**Goal**: Iteratively form and test hypotheses until root cause is identified or circuit breaker triggers.

Initialize: `_hypothesis_count = 0`

### 2.1 Form Hypothesis

Based on available evidence (Symptom Record, suspect code, Change History, Dependency Map), state:
- **Hypothesis**: One sentence describing the suspected root cause.
- **Confidence**: Level 2–5 (from epistemic calibration).
- **Probe Design**: A specific, read-only action to confirm or refute.

### 2.2 Execute Probe

Allowed probe types (read-only + test execution):
- `Read` a specific code region
- `Grep` for a pattern
- `Bash`/`PowerShell`: re-run a test, print environment variables, `git blame`, `git log`, execute diagnostic one-liners
- **Prohibited**: file writes, file deletions, network requests, process management (kill/stop)

### 2.3 Evaluate Result

- **Confirmed** (evidence supports hypothesis): Record as **Root Cause Candidate**. Proceed to Phase 3.
- **Refuted** (evidence contradicts hypothesis): Record in **Exclusion Log**. Increment `_hypothesis_count`.
- **Inconclusive** (evidence neither confirms nor refutes): Gather additional evidence. Do NOT increment counter.

### 2.4 Circuit Breaker Check

If `_hypothesis_count >= DEBUG_MAX_HYPOTHESES`:

1. **HALT** autonomous investigation.
2. Present to user via `AskUserQuestion`:
   - Summary of all tested hypotheses and why each was refuted.
   - The Exclusion Log.
   - Options:
     - "提供新方向 (Provide new direction)" — user gives additional context, reset counter to 0, resume loop.
     - "增加尝试次数 (Increase limit)" — double the limit, resume loop.
     - "输出当前诊断 (Output current diagnosis)" — proceed to Phase 3 with status `inconclusive`.

### 2.5 Loop

Return to 2.1 with updated evidence context.

**Phase 2 Exit**: Either a Root Cause Candidate is confirmed, or user chose to output current diagnosis.

---

## Phase 3: Diagnosis Report

**Goal**: Produce a structured, readable diagnosis report.

1. `Read` the template: `skills/remy-debug/diagnosis_template.md`.
2. Fill in all sections with collected evidence.
3. Ensure directory: `Bash("mkdir -p '.claude/temp_debug'")`.
4. Get timestamp: `Bash("date +\"%Y%m%d_%H%M%S\"")` → `{TIMESTAMP}`.
5. `Write` the report to `.claude/temp_debug/debug_{TIMESTAMP}.md`.

---

## Phase 4: Packet Generation

**Goal**: Produce an evidence packet compatible with `/remy-patch`.

1. Get git commit (if available): `Bash("git rev-parse HEAD 2>/dev/null || echo NO_GIT")`.
2. Ensure directory: `Bash("mkdir -p '.claude/temp_task'")`.
3. `Write` packet to `.claude/temp_task/debug_{TIMESTAMP}.json`:

```json
{
  "v": "1.0.0",
  "task": {
    "id": "debug_{TIMESTAMP}",
    "mode": "write",
    "summary": "<one sentence: what to fix based on diagnosis>",
    "read_only_until_evidence": true
  },
  "sender_payload": {
    "plan": ["<proposed fix step 1>", "<proposed fix step 2>"],
    "analysis": "<constraints and risks from diagnosis>",
    "assumptions": ["<any unconfirmed hypothesis marked as assumption>"]
  },
  "evidence_packet": {
    "source_revision": {
      "type": "git",
      "commit": "{COMMIT}",
      "retrieved_at": "<ISO-8601 datetime>"
    },
    "evidence": [
      {
        "id": "E-001",
        "file_type": "source",
        "path": "<repo-relative path>",
        "range": {"start": 1, "end": 50},
        "why": "<why this evidence is relevant to the root cause>",
        "status": "confirmed",
        "confidence": 0.9,
        "excerpt": "<verbatim text from that range>"
      }
    ],
    "proposed_changes": [
      {
        "id": "C-001",
        "description": "<fix description derived from root cause>",
        "evidence_refs": ["E-001"]
      }
    ]
  }
}
```

**Strict Rules:**
- `evidence[]`: one item per file ACTUALLY READ during this session. Unread files MUST NOT appear.
- `excerpt`: MANDATORY verbatim text. Summaries are prohibited.
- `status`: `"confirmed"` for files read; `"suspected"` for inferred but unread files.
- If NOT a git repo: use `"type": "filesystem"` and omit `"commit"`.
- If diagnosis is `inconclusive`: set `"mode": "investigate"` instead of `"write"` in the task object.

4. Update `.active_packet`: `Bash("rm -f '.claude/temp_task/.active_packet' && echo 'debug_{TIMESTAMP}.json' > '.claude/temp_task/.active_packet'")`.

---

## Phase 5: Stop Protocol (Mandatory)

After writing the report and packet:

1. Print the diagnosis summary (root cause or inconclusive status).
2. Print: `📦 Report: debug_{TIMESTAMP}.md | Packet: debug_{TIMESTAMP}.json`
3. Print: `执行修复: /remy-patch debug_{TIMESTAMP}.json` (only if mode = "write").
4. **STOP**. Do NOT apply any code changes.
