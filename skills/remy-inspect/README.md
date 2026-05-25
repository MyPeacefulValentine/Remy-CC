# remy-inspect (Post-Modification Verification)

remy-inspect discovers existing tests, creates temporary tests for uncovered code, runs them, evaluates branch coverage, and audits assertion quality. It operates after code modification — complementary to TDD, which operates before implementation.

Supports multi-angle defect prediction and semantic test quality audit via parallel agents, inspired by the `/code-review` multi-perspective pattern.

## When to Use

- After `/remy-patch`, before generating a changelog
- When verifying that code changes are covered by tests
- When assessing test quality for modified functions

## Usage

```
/remy-inspect [effort] [target_files_or_functions...]
```

### Effort Levels

| Level | Prediction Angles | Audit Angles | Agents | Description |
| :--- | :--- | :--- | :--- | :--- |
| `low` | 0 | 0 (regex only) | 0 | Fast mode, no agents. Same as pre-v2 behavior. |
| `medium` (default) | 2 | 1 | 3 | Input boundary + error path prediction; coverage gap audit. |
| `high` | 3 | 3 | 6 | Adds state/interaction prediction; assertion strength + test isolation audit. |

## Workflow

### Phase 1: Scope Identification

Determines what changed via `git diff` or user-specified targets. Builds a change set of modified/added functions, classes, and methods.

### Phase 2: Test Discovery

Loads detection rules from `frameworks.json` to identify the project's test framework (pytest, jest, go test, etc.). Maps each changed symbol to existing test files via grep.

### Phase 2.5: Defect Prediction (medium/high only)

Spawns parallel agents to independently analyze the changed code from different perspectives:

- **Angle A — Input Boundary**: Identifies edge-case inputs (null, empty, overflow, type confusion) that may trigger incorrect behavior.
- **Angle B — Error Path**: Identifies untested exception handlers, fallback logic, and I/O failure paths.
- **Angle C — State/Interaction** (high only): Identifies concurrency risks, implicit preconditions, and partial-failure scenarios.

Each agent outputs up to 6 structured failure scenarios. Results are merged, deduplicated, and sorted by priority. The scenario list drives targeted test generation in Phase 3.

### Phase 3: Test Creation (Conditional)

For symbols with no existing test coverage — or when Phase 2.5 produced scenarios — generates temporary tests. When scenarios are available, tests target specific failure conditions with concrete inputs derived from prediction results.

Test requirements: one assertion per behavior, public interface only, deterministic, no mocks unless external I/O.

### Phase 4: Execution & Fix Loop

Runs tests and triages failures. The triage decision tree determines whether a failure is a test defect or an implementation defect before attempting fixes. Tracks prediction accuracy: how many predicted scenarios were confirmed by actual test failures.

### Phase 5: Coverage Assessment

Measures branch coverage of changed functions (via coverage tool or static analysis). Threshold: ≥ 80%. Below-threshold symbols trigger additional test creation.

### Phase 6: Quality Audit (Two Layers)

**Layer 1 — Regex Anti-Patterns** (always): Scans test files for patterns in `anti_patterns.json` (tautological assertions, mock-only testing, etc.).

**Layer 2 — Semantic Audit** (medium/high): Parallel agents evaluate test quality:

- **Angle A — Coverage Gap**: Detects tests that pass but fail to verify meaningful behavior (name-assertion mismatch, type-only checks, side-effect blindness).
- **Angle B — Assertion Strength** (high only): Detects assertions too weak to catch realistic bugs.
- **Angle C — Test Isolation** (high only): Detects hidden inter-test dependencies that cause non-deterministic failures.

Results from both layers are merged and deduplicated. Critical findings block passage.

### Phase 7: Cleanup

Deletes all temporary test files created in Phase 3.

### Phase 8: Report

Saves a structured report to `.claude/temp_inspect/report_{timestamp}.md` including prediction accuracy metrics, semantic audit findings, and the standard coverage/results summary.

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `POST_VERIFY_MAX_RETRIES` | `-1` (unlimited) | Maximum test-fix iterations. `-1` = no limit. |
| `POST_VERIFY_EFFORT` | `medium` | Fallback effort level when not specified as argument. |

## File Structure

| File / Directory | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `frameworks.json` | Test framework detection rules (user-extensible) |
| `anti_patterns.json` | Assertion anti-pattern rules (user-extensible) |
| `prompts/` | Agent prompt templates for prediction and audit angles |
| `schemas/` | JSON schemas defining agent output format |
| `templates/` | Jinja2 templates for temporary test generation and reports |
| `render.py` | Template rendering helper (Jinja2 with fallback) |
