# Post-Verify (Test Verification)

Post-Verify discovers existing tests, creates temporary tests for uncovered code, runs them, evaluates branch coverage, and audits assertion quality. It operates after code modification — complementary to TDD, which operates before implementation.

## When to Use

- After `/code-modification`, before generating a changelog
- When verifying that code changes are covered by tests
- When assessing test quality for modified functions

## Workflow

### Phase 1: Scope Identification

Determines what changed via `git diff` or user-specified targets. Builds a change set of modified/added functions, classes, and methods.

### Phase 2: Test Discovery

Loads detection rules from `frameworks.json` to identify the project's test framework (pytest, jest, go test, etc.). Maps each changed symbol to existing test files via grep.

### Phase 3: Test Creation (Conditional)

For symbols with no existing test coverage, generates temporary tests using Jinja2 templates (`test_python.py.j2`, `test_javascript.js.j2`, `test_go.go.j2`). Temporary tests are placed in `/tmp/` or the project directory depending on import requirements, and are deleted after verification.

Test requirements: one assertion per behavior, public interface only, at least 1 happy-path + 1 edge-case + 1 error-case, no mocks unless external I/O.

### Phase 4: Execution & Fix Loop

Runs tests and triages failures. The triage decision tree determines whether a failure is a test defect or an implementation defect before attempting fixes. Each fix requires user confirmation via `AskUserQuestion`. The loop respects `POST_VERIFY_MAX_RETRIES`.

### Phase 5: Coverage Assessment

Measures branch coverage of changed functions (via coverage tool or static analysis). Threshold: ≥ 80%. Below-threshold symbols trigger additional test creation.

### Phase 6: Assertion Quality Audit

Scans test files for anti-patterns defined in `anti_patterns.json` (tautological assertions, mock-only testing, etc.). Critical findings block passage.

### Phase 7: Cleanup

Deletes all temporary test files created in Phase 3.

### Phase 8: Report

Saves a structured report to `.claude/temp_test/report_{timestamp}.md` and prints a summary.

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `POST_VERIFY_MAX_RETRIES` | `-1` (unlimited) | Maximum test-fix iterations. `-1` = no limit. |

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `frameworks.json` | Test framework detection rules (user-extensible) |
| `anti_patterns.json` | Assertion anti-pattern rules (user-extensible) |
| `templates/` | Jinja2 templates for temporary test generation |
| `render.py` | Template rendering helper (Jinja2 with fallback) |
