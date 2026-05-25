# remy-test

Generate persistent unit tests for existing or stub code. Supports post-hoc testing (default) and TDD mode (`--tdd`). Multi-angle agent analysis at medium/high effort levels.

## Usage

```
/remy-test [low|medium|high] [--tdd [packet_file]] [target_files...]
```

### Modes

- **Post-hoc** (default): Reads existing implementation and generates tests that validate current behavior. Tests are expected to PASS.
- **TDD** (`--tdd`): Generates failing test skeletons from interface signatures or `/remy-plan` evidence packets. Tests are expected to FAIL (RED state).

### Effort Levels

| Effort | Strategy | Agents |
| :--- | :--- | :--- |
| low | Heuristic: signatures + docstrings | 0 |
| medium | Behavioral Contract (A) + Boundary Exploration (B) | 2 |
| high | A + B + Property-Based Testing (C) | 3 |

### Examples

```bash
/remy-test                           # Auto-detect changed files, medium effort
/remy-test high src/auth.py          # High effort on specific file
/remy-test --tdd                     # TDD mode, detect stubs in changed files
/remy-test --tdd task_20260525.json  # TDD mode with remy-plan packet
/remy-test low src/utils.py          # Quick heuristic-only generation
```

## Workflow Chain

```
/remy-plan → /remy-test --tdd {packet} → /remy-patch {packet} → /remy-inspect
```

## Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `TEST_GEN_EFFORT` | `medium` | Default effort level |
| `TEST_COVERAGE_THRESHOLD` | `80` | Branch coverage target (shared with `/remy-inspect`) |
| `TEST_COVERAGE_MAX_SUPPLEMENT_ROUNDS` | `3` | Max coverage supplement iterations |

## Output

- **Test files**: Written to the project's test directory (auto-detected or user-specified).
- **Report**: `.claude/temp_test/testgen_{timestamp}.md`
- **Coverage report** (if supplement declined): `.claude/temp_test/coverage_{timestamp}.md`
- **TDD packet** (TDD mode only): `.claude/temp_task/testgen_{timestamp}.json` — pass to `/remy-patch`.

## External Files

| File | Purpose |
| :--- | :--- |
| `frameworks.json` | Test framework detection rules. User-extensible. |
| `schemas/test_scenario.json` | Agent output schema. |
| `prompts/generate_behavioral.md` | Agent A: behavioral contract analysis. |
| `prompts/generate_boundary.md` | Agent B: boundary exploration. |
| `prompts/generate_property.md` | Agent C: property-based testing (high only). |
| `templates/test_python.py.j2` | Python test template. |
| `templates/test_typescript.ts.j2` | TypeScript test template. |
| `templates/test_go.go.j2` | Go test template. |
| `render.py` | Template rendering helper (Jinja2 with built-in fallback). |
