---
name: remy-ci
description: Analyze CI/CD failure logs to diagnose build, test, and gate failures. Supports GitHub Actions (gh CLI), local log files, and pasted logs. Produces evidence packets for /remy-patch.
allowed-tools: Read, Grep, Glob, Bash, PowerShell, Write, AskUserQuestion
argument-hint: "[run_id | log_file_path | --paste]"
disable-model-invocation: true
---

# Remy CI Protocol

CI/CD failure log analysis skill. Parses build/test/gate failure logs, localizes errors to source files, and produces a structured diagnosis report plus an evidence packet compatible with `/remy-patch`. Does NOT modify source code.

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `CI_LOG_MAX_LINES` | `500` | Maximum lines to retain per failed step when truncating logs. |

### Argument Parsing

```
/remy-ci [run_id | log_file_path | --paste]
```

1. If argument is a numeric string: treat as **GitHub Actions run ID** (Mode C).
2. If argument is a file path that exists on disk: treat as **local log file** (Mode B).
3. If argument is `--paste`: treat as **paste mode** (Mode A) — prompt user to provide log text.
4. If no argument provided: enter **Guided Mode** (Phase 0.1).

---

## Phase 0: Log Acquisition

**Goal**: Obtain CI failure log content for analysis.

### 0.1 Guided Mode (no arguments)

Use `AskUserQuestion` to ask:
- Input source? Options:
  - "GitHub Actions (gh CLI)" → proceed to 0.2
  - "Local log file" → proceed to 0.3
  - "Paste log text" → proceed to 0.4

### 0.2 GitHub Actions Mode (Mode C)

**Prerequisite Check**:
1. Run `Bash("gh --version 2>/dev/null && echo GH_OK || echo GH_MISSING")`.
2. If `GH_MISSING`: report "gh CLI not available. Use Mode A (paste) or Mode B (file) instead." → return to 0.1.
3. Run `Bash("gh auth status 2>&1 | head -3")` to verify authentication.
4. If not authenticated: report authentication issue → return to 0.1.

**Log Retrieval**:

If run_id was provided:
1. Fetch structured metadata: `Bash("gh run view {run_id} --json jobs,conclusion,name,headBranch")`.
2. Parse JSON output. Identify failed jobs and steps.
3. Fetch failed step logs: `Bash("gh run view {run_id} --log-failed 2>&1 | tail -n {CI_LOG_MAX_LINES}")`.

If no run_id:
1. Detect current branch: `Bash("git branch --show-current")`.
2. Find latest failed run: `Bash("gh run list --branch {branch} --status failure -L 1 --json databaseId,name,conclusion,event")`.
3. If no failed runs found: report "No failed CI runs found on branch {branch}." → HALT.
4. Extract `databaseId` and proceed as if run_id was provided.

Record the structured metadata as **CI Metadata** and the log text as **Raw Log**.

### 0.3 File Mode (Mode B)

1. `Read` the specified file path.
2. If file exceeds `CI_LOG_MAX_LINES` lines: apply Phase 1 truncation strategy.
3. Record content as **Raw Log**. CI Metadata is unavailable.

### 0.4 Paste Mode (Mode A)

1. Use `AskUserQuestion`: "Paste the CI failure log below (or provide the key error messages)."
2. Record user input as **Raw Log**. CI Metadata is unavailable.

**Phase 0 Exit**: Raw Log exists (and optionally CI Metadata). Proceed to Phase 1.

---

## Phase 1: Classification & Truncation

**Goal**: Identify the failure type(s) and reduce log volume.

### 1.1 Structured Classification (Mode C only)

If CI Metadata is available:
1. Parse job/step names to infer failure categories (e.g., step named "Build" → compile_error, step named "Test" → test_failure).
2. Map each failed step to a failure type from `parsers.json`.
3. If mapping succeeds: skip keyword classification (1.2) for those steps.

### 1.2 Keyword Classification (All modes, fallback)

1. `Read` the file `skills/remy-ci/parsers.json`.
2. For each registered parser entry, scan the Raw Log for its `keywords` (case-insensitive).
3. Count keyword matches per type. Assign the type(s) with the most matches.
4. If no keywords match any type: assign `unknown`.

### 1.3 Log Truncation

If Raw Log exceeds `CI_LOG_MAX_LINES`:
1. Extract all lines matching error/failure keywords across all matched parser types.
2. Extract the last `CI_LOG_MAX_LINES / 2` lines of the Raw Log (tail context).
3. Merge (deduplicate) these two sets, preserving original line order.
4. Record the result as **Filtered Log**.

If Raw Log is within limits: use Raw Log directly as **Filtered Log**.

**Phase 1 Exit**: Failure type(s) identified. Filtered Log available. Proceed to Phase 2.

---

## Phase 2: Type-Specific Parsing

**Goal**: Extract structured error information from the Filtered Log.

For each identified failure type, apply the corresponding parsing rules:

### 2.1 Compile Error (`compile_error`)
- Pattern: `{file}:{line}:{col}: error: {message}` or `{file}:{line}: error: {message}`
- Extract: file path, line number, column (if present), error message.

### 2.2 Link Error (`link_error`)
- Pattern: `undefined reference to '{symbol}'`, `multiple definition of '{symbol}'`
- Extract: symbol name, referencing object file (if present).

### 2.3 Test Failure (`test_failure`)
- Patterns: pytest (`FAILED {test_id}`), gtest (`[  FAILED  ] {test_name}`), kunit (`not ok {n} - {test_name}`), TAP format.
- Extract: test name, assertion message, file path and line (if present).

### 2.4 QEMU/Emulation Failure (`qemu_failure`)
- Patterns: `Kernel panic`, `BUG:`, `Unable to mount root fs`, `timeout`, QEMU exit codes.
- Extract: panic message, call trace (if present).

### 2.5 Style Check (`style_check`)
- Patterns: checkpatch (`WARNING:`, `ERROR:`), clang-format diff output.
- Extract: file path, line number, rule name, message.

### 2.6 Sanitizer Report (`sanitizer_report`)
- Patterns: `BUG: KASAN:`, `UBSAN:`, `BUG: KCSAN:`, AddressSanitizer/ThreadSanitizer.
- Extract: bug type, file path, line number, stack trace summary.

### 2.7 Static Analysis (`static_analysis`)
- Patterns: sparse (`warning:`), smatch, Coverity (`CID`), clang-tidy (`[check-name]`).
- Extract: tool name, file path, line number, finding description.

### 2.8 Build Config (`build_config`)
- Patterns: `No rule to make target`, `CONFIG_* not set`, `unmet direct dependencies`.
- Extract: missing target/config, dependency chain.

For each parsed error, produce an **Error Record**: `{type, file, line, message, raw_text}`.

**Phase 2 Exit**: List of Error Records. Proceed to Phase 3.

---

## Phase 3: Localization & Impact

**Goal**: Map Error Records to local workspace files and analyze impact.

### 3.1 File Mapping

For each Error Record with a file path:
1. Normalize the path (strip CI-specific prefixes like `/home/runner/work/repo/repo/`).
2. Check if the file exists locally: `Bash("test -f '{normalized_path}' && echo EXISTS || echo MISSING")`.
3. If EXISTS: `Read` the relevant lines (error line ± 10 lines context).
4. If MISSING: record as "file not found in local workspace".

### 3.2 Git Diff Correlation

1. Get recent changes: `Bash("git diff HEAD~3..HEAD --name-only 2>/dev/null")`.
2. Cross-reference changed files with Error Records.
3. For files that appear in both: `Bash("git diff HEAD~3..HEAD -- '{file}'")` to show what changed.

### 3.3 Impact Analysis (Conditional)

1. Check: `Bash("test -f .claude/logic_index.db && echo EXISTS || echo MISSING")`.
2. **EXISTS**: Collect all unique file paths from Error Records, then run:
   ```
   Bash("python \"~/.claude/skills/remy-index/impact.py\" {file_1} {file_2} ...")
   ```
   Record output as **Impact Report**.
3. **MISSING**: Skip impact analysis.

**Phase 3 Exit**: Error Records enriched with local context, git correlation, and optional impact data. Proceed to Phase 4.

---

## Phase 4: Diagnosis Report

**Goal**: Produce a structured, readable diagnosis report.

1. `Read` the template: `skills/remy-ci/diagnosis_template.md`.
2. Fill in all sections with collected evidence.
3. Ensure directory: `Bash("mkdir -p '.claude/temp_ci'")`.
4. Get timestamp: `Bash("date +\"%Y%m%d_%H%M%S\"")` → `{TIMESTAMP}`.
5. `Write` the report to `.claude/temp_ci/ci_{TIMESTAMP}.md`.

---

## Phase 5: Packet Generation

**Goal**: Produce an evidence packet compatible with `/remy-patch`.

1. Get git commit (if available): `Bash("git rev-parse HEAD 2>/dev/null || echo NO_GIT")`.
2. Ensure directory: `Bash("mkdir -p '.claude/temp_task'")`.
3. `Write` packet to `.claude/temp_task/ci_{TIMESTAMP}.json`:

```json
{
  "v": "1.0.0",
  "task": {
    "id": "ci_{TIMESTAMP}",
    "mode": "write",
    "summary": "<one sentence: what to fix based on CI failure diagnosis>",
    "read_only_until_evidence": true
  },
  "sender_payload": {
    "plan": ["<proposed fix step 1>", "<proposed fix step 2>"],
    "analysis": "<constraints and risks from diagnosis>",
    "assumptions": ["<any inferred but unconfirmed information>"]
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
        "why": "<why this file is relevant to the CI failure>",
        "status": "confirmed",
        "confidence": 0.9,
        "excerpt": "<verbatim text from that range>"
      }
    ],
    "proposed_changes": [
      {
        "id": "C-001",
        "description": "<fix description derived from CI failure analysis>",
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
- If diagnosis could not identify a fixable issue (e.g., infrastructure failure, flaky test): set `"mode": "investigate"` instead of `"write"`.

4. Update `.active_packet`: `Bash("rm -f '.claude/temp_task/.active_packet' && echo 'ci_{TIMESTAMP}.json' > '.claude/temp_task/.active_packet'")`.

---

## 6. Stop Protocol (Mandatory)

After writing the report and packet:

1. Print the diagnosis summary (failure types found, root causes identified, files affected).
2. Print: `📦 Report: ci_{TIMESTAMP}.md | Packet: ci_{TIMESTAMP}.json`
3. Print: `执行修复: /remy-patch ci_{TIMESTAMP}.json` (only if mode = "write").
4. **STOP**. Do NOT apply any code changes.
