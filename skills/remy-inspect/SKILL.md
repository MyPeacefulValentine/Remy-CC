---
name: remy-inspect
description: Discover and run tests after code modification. Supports multi-angle defect prediction and coverage analysis. Recommended after large-scale edits or accumulated working tree changes.
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, AskUserQuestion, Agent
argument-hint: "[low|medium|high] [target_files or changed_functions (optional)]"
disable-model-invocation: true
---

# Post-Verify Protocol

Post-implementation verification skill. Discovers existing tests, creates temporary tests when none exist, runs them, evaluates coverage and assertion quality, and drives a fix loop until all tests pass.

Supports three effort levels for multi-angle analysis:
- **low**: Skip prediction/semantic audit; direct test discovery and execution (fastest).
- **medium** (default): 2 prediction angles + 1 semantic audit angle (3 parallel agents).
- **high**: 3 prediction angles + 3 semantic audit angles (6 parallel agents).

**Relationship to TDD**: TDD operates *before* implementation (RED→GREEN→REFACTOR). This skill operates *after* implementation to verify completed changes. They are complementary, not overlapping.

## External Files

> **Path Convention**: All paths below are relative to `~/.claude/`. Use `Read("~/.claude/skills/remy-inspect/...")` to access them — they are NOT in the project working directory.

| File | Purpose |
| :--- | :--- |
| `skills/remy-inspect/frameworks.json` | Test framework detection rules (Phase 2). User-extensible. |
| `skills/remy-inspect/anti_patterns.json` | Assertion anti-pattern detection rules (Phase 7.1). User-extensible. |
| `skills/remy-inspect/schemas/prediction_scenario.json` | Output schema for defect prediction agents (Phase 3). |
| `skills/remy-inspect/schemas/audit_finding.json` | Output schema for semantic audit agents (Phase 7.2). |
| `skills/remy-inspect/prompts/predict_input_boundary.md` | Prediction Angle A prompt template. |
| `skills/remy-inspect/prompts/predict_error_path.md` | Prediction Angle B prompt template. |
| `skills/remy-inspect/prompts/predict_state_interaction.md` | Prediction Angle C prompt template (high only). |
| `skills/remy-inspect/prompts/audit_coverage_gap.md` | Audit Angle A prompt template. |
| `skills/remy-inspect/prompts/audit_assertion_strength.md` | Audit Angle B prompt template (high only). |
| `skills/remy-inspect/prompts/audit_test_isolation.md` | Audit Angle C prompt template (high only). |
| `skills/remy-inspect/templates/test_python.py.j2` | Jinja2 template for temporary Python tests (Phase 4). |
| `skills/remy-inspect/templates/test_javascript.js.j2` | Jinja2 template for temporary JavaScript tests (Phase 4). |
| `skills/remy-inspect/templates/test_go.go.j2` | Jinja2 template for temporary Go tests (Phase 4). |
| `skills/remy-inspect/templates/test_c.c.j2` | Jinja2 template for temporary C tests (multi-framework: kunit/cmocka/Unity/criterion/plain). |
| `skills/remy-inspect/templates/report.md.j2` | Jinja2 template for the final report (Phase 9). |
| `skills/remy-inspect/render.py` | Template rendering helper. Uses Jinja2 when available, falls back to `str.format()`. |

## Optional Dependency: Jinja2

`render.py` attempts `import jinja2`. If unavailable, all templates are rendered via built-in string formatting (functionally equivalent, but templates are not externally editable in fallback mode). Jinja2 is optional; install it with `pip install jinja2`.

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `POST_VERIFY_MAX_RETRIES` | `-1` (unlimited) | Maximum test-fix iterations. `-1` = no limit. Positive integer = hard cap. |
| `POST_VERIFY_EFFORT` | `medium` | Fallback effort level when not specified as argument. |
| `TEST_COVERAGE_THRESHOLD` | `80` | Branch coverage percentage target. Shared with `/remy-testgen`. |

### Argument Parsing

```
/remy-inspect [effort] [target_files_or_functions...]
```

1. If the first argument matches `low`, `medium`, or `high` (case-insensitive): use it as effort level, remaining args are targets.
2. Otherwise: use `POST_VERIFY_EFFORT` env var (default `medium`), all args are targets.

### Effort Level Matrix

| Effort | Prediction Angles | Semantic Audit Angles | Total Agents |
| :--- | :--- | :--- | :--- |
| low | 0 (skip Phase 3) | 0 (regex only in Phase 7) | 0 |
| medium | 2 (A: Input Boundary, B: Error Path) | 1 (A: Coverage Gap) | 3 |
| high | 3 (A + B + C: State/Interaction) | 3 (A + B: Assertion Strength + C: Test Isolation) | 6 |

## Report Persistence

Final reports are saved to `.claude/temp_inspect/report_{timestamp}.md` in the project directory via `render.save_report()`. This directory is created automatically if absent.

---

## Phase 1: Scope Identification

**Goal**: Determine what changed and what needs testing.

1. **If arguments provided**: Use them as the target scope (file paths or function names).
2. **If no arguments**: Run `git diff --name-only HEAD` (or `git diff --cached --name-only` if staged). If not a git repo, ask the user to specify targets via `AskUserQuestion`.
3. **Extract change units**: For each changed file, identify modified/added functions, classes, and methods by diffing against the previous version.
4. **Record**: Build an internal `change_set` list: `[{file, symbol, type}]`.

**Output**: Print the change set as a summary table to the user.

---

## Phase 2: Test Discovery

**Goal**: Find existing tests that cover the change set.

### 2.1 Detection Strategy

Load detection rules from `frameworks.json`. Each entry defines:
- `indicators`: file existence or file-content checks, evaluated in `priority` order.
- `run_command` / `coverage_command`: command templates with `{test_files}`, `{module}`, `{package}`, `{test_pattern}` placeholders.
- `test_file_pattern` / `test_dir_patterns`: glob patterns for locating test files.

Execute indicator checks for each framework entry in priority order. Stop at the first match.

If no framework is detected and no test directories exist → proceed to Phase 3 (or Phase 4 if effort=low).

### 2.2 Mapping Changes to Tests

For each item in `change_set`:

1. **Grep** for the symbol name in test directories.
2. **Grep** for import/require statements referencing the changed file.
3. **Record** mapping: `{symbol → [test_file:test_function]}`.
4. **Flag uncovered symbols**: Symbols with zero test references.

**Output**: Print a coverage mapping table. Flag uncovered symbols.

---

## Phase 3: Defect Prediction (Multi-Angle) (Trigger: effort ≥ medium)

**Trigger**: effort level is `medium` or `high`. Skip entirely when effort is `low`.

**Goal**: Use parallel agents to identify failure scenarios from multiple independent perspectives, driving targeted test generation in Phase 4.

### 3.1 Context Preparation

Gather the following context for agent prompts:
1. **Diff**: `git diff HEAD` output for the target files (or full source if no prior version).
2. **Source**: Full source of each function in `change_set`.
3. **Existing tests**: List of test function names discovered in Phase 2 (to avoid duplicating coverage).
4. **Callers/callees** (optional): If `.claude/logic_index.db` exists, run `impact.py` and include the upstream/downstream summary. MCP alternative: `query_callers` / `query_callees` tools if `remy-index` server is active.

### 3.2 Agent Dispatch

Read the prompt templates from `~/.claude/skills/remy-inspect/prompts/`:

| Effort | Agents Launched (in parallel) |
| :--- | :--- |
| medium | `predict_input_boundary.md` (Angle A) + `predict_error_path.md` (Angle B) |
| high | A + B + `predict_state_interaction.md` (Angle C) |

For each angle, construct the Agent call:

```
Agent({
  description: "Post-verify: [angle name]",
  prompt: "[prompt template content]\n\n---\n\n## Provided Context\n\n### Diff\n```\n{diff}\n```\n\n### Source\n```\n{source}\n```\n\n### Existing Tests\n{test_names}\n\n### Caller/Callee Context\n{impact_summary}"
})
```

Launch all agents **in parallel** (single message, multiple Agent tool calls).

### 3.3 Result Processing

1. **Parse**: Extract JSON array from each agent's response. If parsing fails (malformed JSON), discard that angle's results with a warning.
2. **Merge**: Concatenate all scenario arrays.
3. **Dedup**: For scenarios with the same `symbol` + similar `scenario` text (>80% token overlap), keep the one with higher priority.
4. **Sort**: Order by priority (high → medium → low).
5. **Record**: Store as `scenario_list` for Phase 4 consumption.

**Output**: Print the merged scenario list as a table:

| # | Symbol | Category | Scenario | Priority |
| :--- | :--- | :--- | :--- | :--- |

---

## Phase 4: Test Creation (Conditional)

**Trigger**: Any symbol in `change_set` has no existing test coverage, OR `scenario_list` is non-empty.

### 4.1 Scenario-Driven Generation (when scenario_list available)

When Phase 3 produced scenarios:
1. For each scenario in `scenario_list`, generate a test targeting that specific failure condition.
2. Test naming: `test_{symbol}_{category_short}_{scenario_slug}`
   - `category_short`: `boundary`, `errpath`, `state`
   - `scenario_slug`: 2-3 word kebab-case summary
3. Use `trigger_input` as the test's input setup.
4. Use `expected_behavior` to derive the assertion.
5. `priority=high` scenarios are generated first.

### 4.2 Placement Strategy (Mixed)

| Condition | Location | Cleanup |
| :--- | :--- | :--- |
| Module importable without project-specific setup | `/tmp/_postverify_{timestamp}/` | Delete entire directory after Phase 8 |
| Module requires project structure (relative imports, config files, fixtures) | Project directory: `_temp_postverify_test_{timestamp}.py` | Delete file after Phase 8 |

### 4.3 Template-Based Generation

Use `render.render_template()` to generate test files from the appropriate language template (`test_python.py.j2`, `test_javascript.js.j2`, `test_go.go.j2`, `test_c.c.j2`).

**Python / JavaScript / Go:**

```python
{
    "module_name": "...",
    "imports": ["import ...", ...],
    "test_cases": [
        {
            "name": "function_name_scenario_expected",
            "description": "...",
            "body_lines": ["result = func(arg)", "assert result == expected"],
            "is_async": False
        }
    ]
}
```

**C (additional keys):**

```python
{
    "framework": "kunit|cmocka|unity|criterion|plain_c",
    "module_name": "...",
    "suite_name": "...",
    "includes": ['"header.h"', "<system.h>"],
    "test_cases": [
        {
            "name": "test_func_scenario_expected",
            "description": "...",
            "body_lines": ["KUNIT_EXPECT_EQ(test, 2, add(1, 1));"]
        }
    ]
}
```

The `framework` value is determined by Phase 2 detection. `suite_name` is derived from `module_name` (e.g., `my_module` → `my_module_test`). `includes` replaces `imports` for C — each entry is a literal `#include` argument (with quotes or angle brackets).

If the target language has no matching template, generate tests directly via LLM (no template).

### 4.4 Test Quality Requirements

Temporary tests MUST satisfy:

- **One assertion per logical behavior**. No multi-behavior bundling.
- **Test the public interface**, not internal state.
- **Include at least** (when no scenario_list):
  - 1 happy-path case
  - 1 edge-case (empty input, boundary value, None/null)
  - 1 error-case (invalid input, expected exception)
- **No mocks** unless the dependency is external I/O (network, filesystem, database). When mocking is unavoidable, mock at the boundary, not deep internals.
- **Deterministic**. No random data without fixed seeds. No time-dependent assertions without freezing time.

### 4.5 Test Naming Convention

```
test_{function_name}_{scenario}_{expected_outcome}
```

Example: `test_load_policy_missing_env_var_returns_always`

---

## Phase 5: Test Execution & Fix Loop

### 5.1 Initial Run

1. **Run** the relevant test command (from `frameworks.json` or constructed for temp tests).
2. **Capture** stdout, stderr, and exit code.
3. **Parse** results: count passed, failed, errored.

### 5.2 Failure Triage (Critical)

When a test fails, determine the fault location **before** attempting any fix. Do NOT default to blaming the implementation.

**Triage decision tree:**

```
Test fails
├─ Is the assertion itself correct?
│   ├─ NO → Test defect. Fix the test.
│   └─ YES ↓
├─ Does the test setup match real-world conditions?
│   ├─ NO → Test defect. Fix setup/fixtures.
│   └─ YES ↓
├─ Is the test importing/calling the correct symbol?
│   ├─ NO → Test defect. Fix import/call.
│   └─ YES ↓
└─ Implementation defect. Fix the code.
```

**Rule**: When triage is ambiguous, report both hypotheses to the user via `AskUserQuestion` and let them decide. Do NOT guess.

### 5.3 Prediction Accuracy Tracking

When `scenario_list` exists: for each scenario-driven test that fails (revealing a real implementation issue), increment `prediction_confirmed` counter. This feeds into Phase 9 accuracy metrics.

### 5.4 Fix Loop

```
iteration = 0
max_retries = POST_VERIFY_MAX_RETRIES (from env, default -1)

LOOP:
  IF max_retries >= 0 AND iteration >= max_retries:
      HALT. Report: "Reached maximum retry limit ({max_retries}). {N} tests still failing."
      EXIT with failure summary.

  1. Triage failure (Section 5.2).
  2. Propose fix via AskUserQuestion:
     - Show: failing test name, error message, triage conclusion (test defect vs. code defect).
     - Options: "Apply fix" / "Skip this failure" / "Abort verification".
  3. IF user approves:
     - Apply fix (Edit tool).
     - Re-run tests.
     - IF all pass → break LOOP.
     - ELSE → iteration += 1, continue LOOP.
  4. IF user skips → continue to next failure.
  5. IF user aborts → EXIT immediately, skip to Phase 8 cleanup.
```

---

## Phase 6: Coverage Assessment

**Trigger**: All tests pass (or user chose to skip remaining failures).

### 6.1 Branch Coverage Measurement

1. **If coverage tool available**: Use the `coverage_command` from `frameworks.json` (e.g., `pytest --cov={module} --cov-branch --cov-report=term-missing`).
2. **If coverage tool unavailable**: Perform static analysis — enumerate branches (if/elif/else, try/except, ternary, loop conditions) in changed functions and check whether tests exercise both sides.
3. **Threshold**: Branch coverage of changed functions/classes >= `TEST_COVERAGE_THRESHOLD`% (default: 80).

### 6.2 Coverage Report

Print a table:

| Symbol | Branches | Covered | Coverage | Status |
| :--- | :--- | :--- | :--- | :--- |
| `load_policy` | 6 | 5 | 83% | PASS |
| `inject_all` | 10 | 7 | 70% | FAIL |

### 6.3 Below Threshold

If any symbol is below `TEST_COVERAGE_THRESHOLD`%:

1. Identify uncovered branches (from `--cov-report=term-missing` or static analysis).
2. **Create additional tests** targeting those branches (same rules as Phase 4).
3. Re-run and re-assess. This counts toward the fix loop iteration limit.

---

## Phase 7: Quality Audit

**Goal**: Verify that passing tests are testing meaningful behavior, not trivially true.

### 7.1 Regex Anti-Pattern Detection (Baseline — Always Runs)

Load detection rules from `anti_patterns.json`. Each entry defines:
- `id`, `name`, `severity` (`critical` / `warning` / `info`)
- `regex`: list of patterns to match against test source
- `negative_regex` (optional): patterns that, if present, negate the match
- `detection`: `"ast_scan"` for rules requiring structural analysis (no regex shortcut)
- `languages`: applicable language filter

For each relevant test file:
1. Filter rules by detected language.
2. Apply regex-based rules via `Grep`.
3. Apply `ast_scan` rules by reading the test file and analyzing structure.
4. For rules with `negative_regex`: flag only if positive matches exist AND no negative matches exist.

### 7.2 Semantic Audit Agents (Medium/High Only)

**Trigger**: effort level is `medium` or `high`. Skip when `low`.

Read prompt templates from `~/.claude/skills/remy-inspect/prompts/`:

| Effort | Agents Launched (in parallel) |
| :--- | :--- |
| medium | `audit_coverage_gap.md` (Angle A) |
| high | A + `audit_assertion_strength.md` (Angle B) + `audit_test_isolation.md` (Angle C) |

For each audit angle, construct the Agent call:

```
Agent({
  description: "Post-verify audit: [angle name]",
  prompt: "[prompt template content]\n\n---\n\n## Provided Context\n\n### Test Source\n```\n{test_code}\n```\n\n### Implementation Source\n```\n{impl_code}\n```\n\n### Test Results\n{pass_fail_summary}\n\n### Change Set\n{change_set_table}"
})
```

Launch all audit agents **in parallel**.

### 7.3 Result Merging

1. **Parse** JSON from each audit agent response. Discard malformed results with a warning.
2. **Merge** regex findings (6.1) and semantic findings (6.2) into a unified list.
3. **Dedup**: If regex and semantic analysis flag the same test+line, keep the semantic version (richer context).
4. **Sort**: critical → warning → info.

### 7.4 Report and Fix

Print findings table. Critical findings from either layer MUST be fixed (following the same AskUser fix loop as Phase 5). Warnings are reported but not blocking.

---

## Phase 8: Cleanup

1. **Delete** all temporary test files created in Phase 4:
   - `/tmp/_postverify_{timestamp}/` — `rm -rf`
   - `_temp_postverify_test_{timestamp}.py` in project directory — `rm -f`
2. **Delete** any `.pyc` / `__pycache__` generated by temp tests in `/tmp`.
3. **Verify** cleanup: `ls` the temp paths to confirm deletion.
4. **Do NOT delete** existing project tests, coverage reports, or any file not created by this skill.

---

## Phase 9: Final Report

Use `render.save_report()` to generate and persist the report to `.claude/temp_inspect/report_{timestamp}.md`.

Populate the context dict:

```python
{
    "project_name": "...",
    "effort_level": "medium",
    "change_set": [{"file": "...", "symbol": "...", "type": "..."}],
    "coverage_map": [{"symbol": "...", "existing_count": N, "temp_count": N}],
    "prediction_scenarios": [
        {"symbol": "...", "scenario": "...", "category": "...", "priority": "...", "test_result": "PASS|FAIL"}
    ],
    "prediction_accuracy": {"total": N, "confirmed_by_test": M},
    "test_results": [{"name": "...", "status": "PASS/FAIL", "duration": "..."}],
    "passed": N,
    "total": N,
    "fix_iterations": N,
    "coverage_data": [{"symbol": "...", "branches": N, "covered": N, "percent": N, "status": "PASS/FAIL"}],
    "audit_findings": [{"id": "...", "pattern_name": "...", "severity": "...", "file": "...", "line": N, "source": "regex|semantic"}],
    "semantic_findings": [{"test_name": "...", "issue": "...", "category": "...", "severity": "...", "evidence": "...", "suggestion": "..."}],
    "final_status": "PASS / FAIL"
}
```

Also print a condensed summary to stdout:

```
Post-Verify Complete
====================
Effort:         {effort_level}
Change Set:     {N} symbols across {M} files
Prediction:     {scenarios} scenarios identified, {confirmed}/{scenarios} confirmed by tests
Tests:          {existing} existing, {created} temporary (cleaned)
Results:        {passed}/{total} passed | Fix iterations: {iterations}
Branch Coverage: {min}% - {max}% (threshold: {TEST_COVERAGE_THRESHOLD}%)
Audit:          {critical} critical, {warning} warnings ({regex_count} regex + {semantic_count} semantic)
Status:         PASS / FAIL
Report:         .claude/temp_inspect/report_{timestamp}.md
```

---

## Critical Rules

1. **Never modify production code without user confirmation** via `AskUserQuestion`.
2. **Never skip failure triage**. Blaming implementation by default is a protocol violation.
3. **Never leave temporary files behind**. Phase 8 is mandatory even on early abort.
4. **Never lower the coverage threshold** below `TEST_COVERAGE_THRESHOLD` (default: 80%). The env var is the single source of truth.
5. **Never trust a passing test without auditing it** (Phase 7). A tautological test is worse than no test.
6. **Clean separation**: This skill does not write permanent tests for the project unless the user explicitly requests it. All created tests are temporary by default.
7. **Agent failure tolerance**: If an Agent call fails (permission denied, timeout, malformed response), log a warning and continue without that angle's results. Do NOT halt the entire workflow.
8. **Effort=low backward compatibility**: When effort is `low`, the skill behaves identically to the pre-v2 version (no agents, no prediction, regex-only audit).
