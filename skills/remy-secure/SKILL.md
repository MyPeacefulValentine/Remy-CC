---
name: remy-secure
description: Review branch changes for security vulnerabilities. Multi-stage: regex pre-scan, parallel agents, and false-positive filtering.
allowed-tools: Read, Grep, Glob, Bash, AskUserQuestion, Agent
argument-hint: "[low|medium|high] [diff_range (optional, e.g. HEAD~3...HEAD)]"
disable-model-invocation: true
---

# Security Audit Protocol

Security-focused review of code changes on the current branch. Identifies exploitable vulnerabilities with high confidence (≥ 8/10), minimizing false positives through a multi-stage pipeline.

Supports three effort levels:
- **low**: Phase 0 regex pre-scan only (0 agents, fastest).
- **medium** (default): Phase 0 + 3 category agents (Injection, Auth, Data Exposure).
- **high**: Phase 0 + 5 category agents (+ Crypto/Secrets, Deserialization/Dynamic Exec).

## External Files

> **Path Convention**: All paths below are relative to `~/.claude/`. Use `Read("~/.claude/skills/remy-secure/...")` to access them.

| File | Purpose |
| :--- | :--- |
| `skills/remy-secure/rules/exclusions.json` | Hard exclusion rules for false-positive filtering. User-extensible. |
| `skills/remy-secure/rules/precedents.json` | Precedent judgments for contextual filtering. User-extensible. |
| `skills/remy-secure/rules/patterns.json` | Phase 0 deterministic regex patterns (dangerous functions, hardcoded secrets). |
| `skills/remy-secure/prompts/scan_injection.md` | Agent A: SQL/Cmd/Path/Template/NoSQL/XXE injection. |
| `skills/remy-secure/prompts/scan_auth.md` | Agent B: Auth bypass/privilege escalation/session/JWT. |
| `skills/remy-secure/prompts/scan_data_exposure.md` | Agent C: PII logging/API leakage/debug exposure. |
| `skills/remy-secure/prompts/scan_crypto.md` | Agent D (high only): Weak algorithms/hardcoded keys/key storage. |
| `skills/remy-secure/prompts/scan_deserialization.md` | Agent E (high only): Pickle/YAML/eval/exec. |
| `skills/remy-secure/prompts/filter_false_positive.md` | False-positive filter agent prompt. |
| `skills/remy-secure/schemas/vulnerability_finding.json` | Output schema for category agents. |
| `skills/remy-secure/schemas/filter_result.json` | Output schema for filter agents. |
| `skills/remy-secure/templates/report.md.j2` | Markdown report template. |
| `skills/remy-secure/render.py` | Template rendering helper. |

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `SECURITY_AUDIT_EFFORT` | `medium` | Fallback effort level when not specified as argument. |
| `SECURITY_AUDIT_MAX_FILTER_AGENTS` | `15` | Maximum parallel filter agents (prevents token explosion on large diffs). |
| `SECURITY_AUDIT_CONFIDENCE_THRESHOLD` | `8` | Minimum confidence (1-10) for a finding to appear in final report. |

### Argument Parsing

```
/remy-secure [effort] [diff_range]
```

1. If the first argument matches `low`, `medium`, or `high` (case-insensitive): use as effort level, remaining arg is diff_range.
2. Otherwise: use `SECURITY_AUDIT_EFFORT` env var (default `medium`), first arg is diff_range.
3. If no diff_range specified: default to `origin/HEAD...HEAD`.

### Effort Level Matrix

| Effort | Phase 0 | Category Agents | Filter Agents | Total Max Agents |
| :--- | :--- | :--- | :--- | :--- |
| low | Yes | 0 | 0 | 0 |
| medium | Yes | 3 (A, B, C) | Up to 15 | Up to 18 |
| high | Yes | 5 (A, B, C, D, E) | Up to 15 | Up to 20 |

---

## 1. Phase 0: Setup & Pre-Scan

### 1.1 Git Repository Discovery

1. Run: `git rev-parse --show-toplevel`
2. If exit code = 0: use the output as `REPO_ROOT`.
3. If exit code ≠ 0: use `AskUserQuestion` to ask the user for the git repository path.
   - If user provides a path: verify it with `git -C <path> rev-parse --show-toplevel`.
   - If verification fails: HALT with error.

### 1.2 Diff Extraction

Using `REPO_ROOT` as the working directory:

1. Run: `git -C <REPO_ROOT> diff --name-only <diff_range>` → file list.
2. Run: `git -C <REPO_ROOT> diff <diff_range>` → full diff content.
3. If diff is empty: report "No changes detected" and EXIT.

### 1.3 Regex Pre-Scan (Deterministic)

Load rules from `rules/patterns.json`. For each pattern:

1. Run `Grep` with the pattern's regex against the changed files.
2. Record matches as `prescan_findings`: `[{id, name, severity, file, line, match}]`.

These findings are:
- Included in the final report directly (they are deterministic, no agent confirmation needed).
- Injected as additional context into category agent prompts (to avoid duplicate reporting).

**If effort = low**: Generate report from `prescan_findings` only and EXIT.

---

## 2. Phase 1: Category Agent Dispatch (Medium/High)

### 2.1 Context Preparation

Gather context for agent prompts:

1. **Diff content**: Full unified diff from Phase 0.
2. **Pre-scan results**: `prescan_findings` list (so agents skip already-flagged patterns).
3. **Impact data** (optional): If `.claude/logic_index.db` exists in the project:
   - Run: `python "~/.claude/skills/remy-index/impact.py" <changed_files...>`
    *   **MCP alternative**: If `remy-index` MCP server is active, `query_impact` / `query_callers` tools provide equivalent data without subprocess overhead.
   - If exit code = 0: include the upstream/downstream summary in agent context.
   - If exit code ≠ 0: skip (graceful degradation).

### 2.2 Agent Dispatch

Read prompt templates from `~/.claude/skills/remy-secure/prompts/`.

| Effort | Agents Launched (in parallel) |
| :--- | :--- |
| medium | `scan_injection.md` (A) + `scan_auth.md` (B) + `scan_data_exposure.md` (C) |
| high | A + B + C + `scan_crypto.md` (D) + `scan_deserialization.md` (E) |

For each agent, construct the Agent call:

```
Agent({
  description: "Security-audit: [category name]",
  prompt: "[prompt template content]\n\n---\n\n## Provided Context\n\n### Diff\n```\n{diff}\n```\n\n### Pre-Scan Findings\n{prescan_json}\n\n### Impact Analysis (Call Graph)\n{impact_summary_or_'Not available'}"
})
```

Launch all agents **in parallel** (single message, multiple Agent tool calls).

### 2.3 Result Parsing

1. Parse JSON array from each agent's response (expected: `vulnerability_finding.json` schema).
2. If parsing fails: log warning, discard that agent's results.
3. Merge all findings into `agent_findings` list.
4. Dedup: If two findings reference the same file + line ± 3 lines + same category, keep the one with higher severity.

---

## 3. Phase 2: False-Positive Filtering (Medium/High)

### 3.1 Finding Selection

1. Sort `agent_findings` by severity (HIGH > MEDIUM > LOW).
2. If count > `SECURITY_AUDIT_MAX_FILTER_AGENTS` (default 15): take top-15 by severity.

### 3.2 Filter Agent Dispatch

Read `filter_false_positive.md` prompt template. For each finding in the selection:

```
Agent({
  description: "Security-audit filter: [file:line]",
  prompt: "[filter template]\n\n---\n\n## Finding to Evaluate\n{finding_json}\n\n## Full Diff Context\n```\n{diff}\n```\n\n## Exclusion Rules\n{exclusions_json}\n\n## Precedent Judgments\n{precedents_json}"
})
```

Launch all filter agents **in parallel**.

### 3.3 Result Processing

1. Parse each filter agent's response (expected: `filter_result.json` schema).
2. Extract `confidence` score (1-10) and `reasoning`.
3. Discard findings with `confidence < SECURITY_AUDIT_CONFIDENCE_THRESHOLD` (default 8).
4. Remaining findings become `verified_findings`.

---

## 4. Phase 3: Report Generation

### 4.1 Merge Results

Combine:
- `prescan_findings` (from Phase 0, always included)
- `verified_findings` (from Phase 2, medium/high only)

Sort by severity (HIGH → MEDIUM → LOW), then by file path.

### 4.2 Output Format

For each finding, output a Markdown block:

```markdown
# Vuln {N}: {Category}: `{file}:{line}`

* Severity: {HIGH|MEDIUM|LOW}
* Confidence: {score}/10
* Description: {description}
* Exploit Scenario: {exploit_scenario}
* Recommendation: {recommendation}
```

### 4.3 Report Persistence

Use `render.save_report()` to persist the report to `.claude/temp_secure/security_audit_{timestamp}.md`.

### 4.4 Summary

Print a condensed summary to stdout:

```
Security Audit Complete
=======================
Effort:          {effort_level}
Diff Range:      {diff_range}
Files Analyzed:  {file_count}
Pre-Scan:        {prescan_count} deterministic findings
Agent Findings:  {agent_count} raw → {verified_count} verified (threshold: {threshold}/10)
Final Report:    {total} findings (HIGH: {h}, MEDIUM: {m}, LOW: {l})
Report:          .claude/temp_secure/security_audit_{timestamp}.md
```

---

## 5. Critical Rules

1. **Read-only by default**: This skill does NOT modify project code. It only reads and reports.
2. **No speculative findings**: Only report findings with confidence ≥ 8/10 in the final output.
3. **Pre-scan is authoritative**: Phase 0 regex matches are deterministic and bypass the filter stage.
4. **Graceful degradation**: If logic_index.db is absent, impact.py fails, or an agent errors, continue without that data source.
5. **Agent failure tolerance**: If a category or filter agent fails (timeout, malformed response), log warning and continue. Do NOT halt the pipeline.
6. **Effort=low backward compatibility**: In low mode, only Phase 0 executes. No agents, no filtering.
7. **Scope constraint**: Only analyze code newly introduced by the diff. Do NOT report pre-existing vulnerabilities in unchanged code.
