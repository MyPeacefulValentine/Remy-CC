---
name: remy-testgen
description: Generate persistent unit tests for existing or stub code. Supports post-hoc testing (default) and TDD mode (--tdd). Multi-angle agent analysis at medium/high effort levels.
allowed-tools: Read, Edit, Write, Grep, Glob, Bash, PowerShell, AskUserQuestion, Agent
argument-hint: "[low|medium|high] [--tdd [packet_file]] [target_files_or_functions (optional)]"
disable-model-invocation: true
---

# Test Generation Protocol

Generate persistent unit tests and write them into the project's test directory. Supports two modes:

- **Post-hoc** (default): Analyze existing implementation code and generate tests that validate current behavior.
- **TDD** (`--tdd`): Generate failing test skeletons from interface signatures or plan packets, before implementation exists.

## External Files

> **Path Convention**: All paths below are relative to `~/.claude/`. Use `Read("~/.claude/skills/remy-testgen/...")` to access them.

| File | Purpose |
| :--- | :--- |
| `skills/remy-testgen/frameworks.json` | Test framework detection rules (Phase 2). User-extensible. |
| `skills/remy-testgen/schemas/test_scenario.json` | Output schema for test generation agents (Phase 3). |
| `skills/remy-testgen/prompts/generate_behavioral.md` | Agent A: Behavioral contract analysis prompt (medium+). |
| `skills/remy-testgen/prompts/generate_boundary.md` | Agent B: Boundary exploration prompt (medium+). |
| `skills/remy-testgen/prompts/generate_property.md` | Agent C: Property-based testing prompt (high only). |
| `skills/remy-testgen/templates/test_python.py.j2` | Jinja2 template for Python test files. |
| `skills/remy-testgen/templates/test_typescript.ts.j2` | Jinja2 template for TypeScript test files. |
| `skills/remy-testgen/templates/test_go.go.j2` | Jinja2 template for Go test files. |
| `skills/remy-testgen/templates/test_c.c.j2` | Jinja2 template for C test files (multi-framework: kunit/cmocka/Unity/criterion/plain). |
| `skills/remy-testgen/templates/report.md.j2` | Jinja2 template for the coverage report. |
| `skills/remy-testgen/render.py` | Template rendering helper. Uses Jinja2 when available, falls back to built-in formatting. |
| `skills/remy-testgen/output_schema.json` | Final output schema (coverage report structure). |

## Optional Dependency: Jinja2

`render.py` attempts `import jinja2`. If unavailable, all templates are rendered via built-in string formatting. Jinja2 can be installed via `install.py` (optional step).

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `TEST_GEN_EFFORT` | `medium` | Fallback effort level when not specified as argument. |
| `TEST_COVERAGE_THRESHOLD` | `80` | Branch coverage percentage target. Shared with `/remy-inspect`. |
| `TEST_COVERAGE_MAX_SUPPLEMENT_ROUNDS` | `3` | Maximum coverage supplement iterations before stopping. |

### Argument Parsing

```
/remy-testgen [effort] [--tdd [packet_file]] [target_files_or_functions...]
```

1. If the first argument matches `low`, `medium`, or `high` (case-insensitive): use it as effort level, remaining args are parsed further.
2. Otherwise: use `TEST_GEN_EFFORT` env var (default `medium`), all args are parsed further.
3. If `--tdd` is present: enable TDD mode. If followed by a filename matching `*.json`, treat it as a remy-plan packet file.
4. Remaining arguments are target file paths or function names.
5. If no targets specified: auto-detect via `git diff --name-only HEAD` (or `git diff --cached --name-only` if staged).

### Effort Level Matrix

| Effort | Generation Strategy | Agents | Coverage Supplement |
| :--- | :--- | :--- | :--- |
| low | Heuristic: signatures + docstrings → happy/edge/error cases | 0 | Disabled |
| medium | 2 parallel agents: Behavioral Contract (A) + Boundary Exploration (B) | 2 | Enabled (ask user) |
| high | 3 parallel agents: A + B + Property-Based Testing (C) | 3 | Enabled (ask user) |

---

## 1. Phase 1: Scope Identification

**Goal**: Determine what code needs test coverage.

### 1.1 Post-hoc Mode

1. **If arguments provided**: Use them as the target scope (file paths or function names).
2. **If no arguments**: Run `git diff --name-only HEAD` (or `git diff --cached --name-only` if staged). If not a git repo, ask the user to specify targets via `AskUserQuestion`.
3. **Extract target symbols**: For each target file, identify all public functions, classes, and methods by reading the source.
4. **Record**: Build `target_set`: `[{file, symbol, type, signature}]`.

### 1.2 TDD Mode

**If `--tdd` with packet file:**
1. `Read` the packet from `.claude/temp_task/{packet_file}`.
2. Extract interface contracts from `sender_payload.plan[]` and `evidence_packet.proposed_changes[]`.
3. Build `target_set` from the described interfaces.

**If `--tdd` without packet file:**
1. Read the target files.
2. Identify stub functions: bodies containing only `pass`, `...`, `raise NotImplementedError`, or `# TODO` comments.
3. Extract function signatures and docstrings from stubs.
4. Build `target_set` from stub signatures.

### 1.3 Impact Analysis (Optional)

1. **Check**: Run `Bash("test -f .claude/logic_index.json && echo EXISTS || echo MISSING")`.
2. **EXISTS**: Run `Bash("python \"~/.claude/skills/remy-index/impact.py\" <target_file_1> ...")`.
   - Record caller/callee context for richer test generation.
   - If exit code = 2: skip (no call graph data).
3. **MISSING**: Skip impact analysis.

**Output**: Print the target set as a summary table.

---

## 2. Phase 2: Framework Detection & Test Discovery

**Goal**: Identify the project's testing conventions.

### 2.1 Framework Detection

Load detection rules from `frameworks.json`. Each entry defines:
- `indicators`: file existence or file-content checks, evaluated in `priority` order.
- `run_command` / `coverage_command`: command templates.
- `test_file_pattern` / `test_dir_patterns`: glob patterns for locating test files.

Execute indicator checks for each framework entry in priority order. Stop at the first match.

If no framework detected: use `AskUserQuestion` to ask the user which framework to target.

### 2.2 Test Directory Discovery

1. Search for existing test directories using `test_dir_patterns` from the matched framework.
2. If found: record as `test_output_dir`.
3. If NOT found: use `AskUserQuestion` to ask the user where to place test files. Options:
   - "Create `tests/` directory" (with framework-appropriate structure)
   - "Place alongside source files" (e.g., `src/foo.py` → `src/test_foo.py`)
   - Custom path

### 2.3 Existing Test Mapping

For each symbol in `target_set`:
1. `Grep` for the symbol name in test directories.
2. `Grep` for import/require statements referencing the target file.
3. Record existing coverage: `{symbol → [test_file:test_function]}`.
4. Flag symbols with zero existing test coverage.

**Output**: Print existing coverage mapping. Flag uncovered symbols.

---

## 3. Phase 3: Test Case Design

**Goal**: Design test cases for uncovered symbols.

### 3.1 Low Effort (Heuristic Generation)

For each uncovered symbol in `target_set`:

1. Read the function source (post-hoc) or signature + docstring (TDD).
2. Generate test cases based on:
   - **Happy path**: One normal-input case that exercises the main code path.
   - **Edge cases**: Empty inputs, boundary values, None/null, single-element collections.
   - **Error cases**: Invalid inputs, expected exceptions, type mismatches.
3. Record as `test_plan`: `[{symbol, test_name, category, description, setup, assertion}]`.

### 3.2 Medium/High Effort (Agent-Assisted)

Read the prompt templates from `~/.claude/skills/remy-testgen/prompts/`:

| Effort | Agents Launched (in parallel) |
| :--- | :--- |
| medium | `generate_behavioral.md` (Agent A) + `generate_boundary.md` (Agent B) |
| high | A + B + `generate_property.md` (Agent C) |

For each agent, construct the Agent call:

```
Agent({
  description: "remy-testgen: [angle name]",
  prompt: "[prompt template content]\n\n---\n\n## Provided Context\n\n### Source\n```\n{source}\n```\n\n### Signatures\n{signatures}\n\n### Existing Tests\n{existing_test_names}\n\n### Caller/Callee Context\n{impact_summary}\n\n### Mode\n{post-hoc|tdd}"
})
```

Launch all agents **in parallel** (single message, multiple Agent tool calls).

### 3.3 Result Processing

1. **Parse**: Extract JSON array from each agent's response using `test_scenario.json` schema. If parsing fails, discard that angle's results with a warning.
2. **Merge**: Concatenate all scenario arrays.
3. **Dedup**: For scenarios targeting the same `symbol` with >80% description overlap, keep the one with higher priority.
4. **Sort**: Order by priority (high → medium → low).
5. **Combine**: Merge agent results with heuristic results (low effort cases). Agent results take precedence on overlap.
6. **Record**: Store as `test_plan`.

**Output**: Print the merged test plan as a table:

| # | Symbol | Test Name | Category | Priority | Source |
| :--- | :--- | :--- | :--- | :--- | :--- |

---

## 4. Phase 4: Test File Generation

**Goal**: Write persistent test files into the project.

### 4.1 File Placement

Using `test_output_dir` from Phase 2:

- **Python**: `{test_output_dir}/test_{source_module}.py`
- **TypeScript**: `{test_output_dir}/{source_module}.test.ts`
- **Go**: `{source_dir}/{source_module}_test.go` (Go convention: same directory)
- **C**: `{test_output_dir}/test_{source_module}.c` (or `{source_dir}/{source_module}_test.c` for kunit)

### 4.2 Conflict Check

Before writing each test file:
1. Check if the target path already exists.
2. If EXISTS: use `AskUserQuestion`:
   - "Append new tests to existing file"
   - "Overwrite existing file"
   - "Write to a different filename"
3. If NOT EXISTS: proceed to write.

### 4.3 Template-Based Generation

Use `render.render_template()` to generate test files. Populate the context dict:

**Python / TypeScript / Go:**

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

### 4.4 Test Naming Convention

```
test_{function_name}_{scenario}_{expected_outcome}
```

Example: `test_load_policy_empty_string_returns_default`

### 4.5 Test Quality Requirements

Generated tests MUST satisfy:

- **One assertion per logical behavior**. No multi-behavior bundling.
- **Test the public interface**, not internal state.
- **No mocks** unless the dependency is external I/O (network, filesystem, database). When mocking is unavoidable, mock at the boundary, not deep internals.
- **Deterministic**. No random data without fixed seeds. No time-dependent assertions without freezing time.

### 4.6 TDD Mode: Stub Expectations

In TDD mode, generated tests MUST:

- Import the target module (even though functions are not yet implemented).
- Assert on expected return values, exception types, or side effects as described in the interface contract.
- Include a comment per test: `# Expected to FAIL until implementation is complete`.

**Output**: Print the list of generated test files and their locations.

---

## 5. Phase 5: Verification Run

**Goal**: Execute generated tests to validate correctness.

### 5.1 Test Execution

1. Use the `run_command` from the detected framework (Phase 2).
2. Capture exit code, stdout, stderr.

### 5.2 Post-hoc Mode: Expect PASS

- If ALL tests pass: proceed to Phase 6.
- If ANY test fails: report the failure. This indicates a defect in the generated test (the implementation is assumed correct in post-hoc mode).
  - Use `AskUserQuestion`:
    - "Fix the generated test" — edit the test file to correct the assertion.
    - "Remove the failing test" — delete the specific test case.
    - "Keep as-is" — user will fix manually.

### 5.3 TDD Mode: Expect FAIL

- If ALL tests fail: confirm RED state. Report: "All tests in RED state. Proceed to implement with `/remy-patch`."
- If ANY test passes: this indicates the function is already implemented (not a true stub).
  - Report which tests passed unexpectedly.
  - Use `AskUserQuestion`:
    - "Keep passing tests" — retain them as regression tests.
    - "Remove passing tests" — they are not needed for TDD.

---

## 6. Phase 6: Coverage Assessment

**Goal**: Measure and report test coverage of the target symbols.

**Trigger**: Post-hoc mode only (TDD mode skips to Phase 7).

### 6.1 Coverage Measurement

Read `TEST_COVERAGE_THRESHOLD` from environment (default: `80`).

1. **If coverage tool available**: Use the `coverage_command` from `frameworks.json`.
2. **If coverage tool unavailable**: Perform static analysis — enumerate branches in target functions and check whether tests exercise both sides.

### 6.2 Coverage Report

Print a table:

| Symbol | Branches | Covered | Coverage | Status |
| :--- | :--- | :--- | :--- | :--- |
| `load_policy` | 6 | 5 | 83% | PASS |
| `inject_all` | 10 | 7 | 70% | FAIL |

### 6.3 Below Threshold

If any symbol is below `TEST_COVERAGE_THRESHOLD`:

1. Use `AskUserQuestion`:
   - "Auto-supplement tests to reach threshold" — proceed to supplement loop (6.4).
   - "Generate coverage report only" — skip to Phase 7.

### 6.4 Supplement Loop

Initialize: `_supplement_round = 0`

```
LOOP:
  IF _supplement_round >= TEST_COVERAGE_MAX_SUPPLEMENT_ROUNDS:
      HALT loop. Report: "Reached supplement limit ({max} rounds). Coverage: {current}%."
      Break to Phase 7.

  1. Identify uncovered branches from coverage output.
  2. Generate additional test cases targeting those branches (same rules as Phase 4).
  3. Append to existing test file.
  4. Re-run tests + coverage.
  5. IF coverage >= threshold for all symbols → break LOOP.
  6. IF no new test cases generated (all branches are impractical to test) → break LOOP.
  7. _supplement_round += 1, continue LOOP.
```

---

## 7. Phase 7: Report & Packet Generation

**Goal**: Produce a persistent report and (in TDD mode) an evidence packet.

### 7.1 Report Generation

1. Ensure directory: `Bash("mkdir -p '.claude/temp_testgen'")`.
2. Get timestamp: `Bash("date +\"%Y%m%d_%H%M%S\"")` → `{TIMESTAMP}`.
3. Use `render.save_report()` to generate and persist the report to `.claude/temp_testgen/testgen_{TIMESTAMP}.md`.

Populate the context dict:

```python
{
    "project_name": "...",
    "mode": "post-hoc|tdd",
    "effort_level": "medium",
    "target_set": [{"file": "...", "symbol": "...", "type": "..."}],
    "test_plan": [{"symbol": "...", "test_name": "...", "category": "...", "priority": "..."}],
    "generated_files": [{"path": "...", "test_count": N}],
    "test_results": [{"name": "...", "status": "PASS/FAIL"}],
    "passed": N,
    "total": N,
    "coverage_data": [{"symbol": "...", "branches": N, "covered": N, "percent": N, "status": "PASS/FAIL"}],
    "supplement_rounds": N,
    "final_status": "PASS / FAIL / RED (TDD)"
}
```

### 7.2 Coverage Report (if supplement was declined)

If the user declined supplement in Phase 6.3:

1. Write coverage report to `.claude/temp_testgen/coverage_{TIMESTAMP}.md`.
2. Include: uncovered branches, suggested test scenarios for each.

### 7.3 TDD Packet Generation (TDD Mode Only)

In TDD mode, produce an evidence packet for `/remy-patch`:

1. Get git commit: `Bash("git rev-parse HEAD 2>/dev/null || echo NO_GIT")`.
2. Write packet to `.claude/temp_task/testgen_{TIMESTAMP}.json`:

```json
{
  "v": "1.0.0",
  "task": {
    "id": "testgen_{TIMESTAMP}",
    "mode": "write",
    "summary": "Implement functions to make generated TDD tests pass",
    "read_only_until_evidence": true
  },
  "sender_payload": {
    "plan": ["Implement {symbol_1} to satisfy test_{name_1}", "..."],
    "analysis": "Tests expect: {interface_contracts_summary}",
    "assumptions": []
  },
  "evidence_packet": {
    "source_revision": {
      "type": "git",
      "commit": "{COMMIT}",
      "retrieved_at": "{ISO-8601}"
    },
    "evidence": [
      {
        "id": "E-001",
        "file_type": "test",
        "path": "{test_file_path}",
        "range": {"start": 1, "end": 50},
        "why": "Generated TDD test defining expected interface behavior",
        "status": "confirmed",
        "confidence": 1.0,
        "excerpt": "{verbatim test content}"
      }
    ],
    "proposed_changes": [
      {
        "id": "C-001",
        "description": "Implement {symbol} to pass {test_count} tests",
        "evidence_refs": ["E-001"]
      }
    ]
  }
}
```

3. Update `.active_packet`: `Bash("rm -f '.claude/temp_task/.active_packet' && echo 'testgen_{TIMESTAMP}.json' > '.claude/temp_task/.active_packet'")`.

---

## 8. Final Summary

Print a condensed summary to stdout:

```
Test Generation Complete
========================
Mode:           {post-hoc|tdd}
Effort:         {effort_level}
Target Set:     {N} symbols across {M} files
Generated:      {test_count} tests in {file_count} files
Results:        {passed}/{total} {passed_text} | Status: {PASS|FAIL|RED}
Coverage:       {min}% - {max}% (threshold: {TEST_COVERAGE_THRESHOLD}%)
Supplement:     {rounds} rounds
Report:         .claude/temp_testgen/testgen_{TIMESTAMP}.md
{Packet:        .claude/temp_task/testgen_{TIMESTAMP}.json | Execute: /remy-patch testgen_{TIMESTAMP}.json}
```

The `Packet` line is printed only in TDD mode.

---

## 9. Critical Rules

1. **Never modify implementation code**. This skill generates tests only. Implementation changes are the responsibility of `/remy-patch` and `/remy-inspect`.
2. **Never overwrite existing test files without user confirmation** via `AskUserQuestion`.
3. **Never generate tests that depend on implementation internals** (private methods, internal state). Test the public interface.
4. **Never lower the coverage threshold below `TEST_COVERAGE_THRESHOLD`**. The env var is the single source of truth.
5. **Never produce non-deterministic tests**. No random data without seeds. No time-dependent assertions without mocking.
6. **Agent failure tolerance**: If an Agent call fails (permission denied, timeout, malformed response), log a warning and continue without that angle's results. Do NOT halt the entire workflow.
7. **Effort=low backward compatibility**: When effort is `low`, no agents are spawned. All test generation is heuristic-based.
8. **TDD packet output**: In TDD mode, packet generation (Phase 7.3) is mandatory. The packet enables the `plan → test → patch` workflow chain.
