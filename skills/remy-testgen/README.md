# remy-testgen — Automated Test Generation Skill

Generate persistent unit tests for existing or stub code. Supports post-hoc testing (default) and TDD mode (`--tdd`). Multi-angle agent analysis at medium/high effort levels.

## Usage

```
/remy-testgen [low|medium|high] [--tdd [packet_file]] [target_files...]
```

### Modes

- **Post-hoc** (default): Distinguishes characterization of current behavior from conformance to explicit requirements. A failure may involve the implementation, generated test or environment; do not assume the implementation is correct or weaken an assertion just to pass.
- **TDD** (`--tdd`): Uses specified interface behavior or the original `/remy-plan` packet. Only failures demonstrating missing required behavior establish RED; passing regression cases may coexist, while syntax/import/environment failures are not business RED.

The existing packet, report fields, effort levels and coverage flow remain. Requirement sources, responsibility and unknowns reach the three agent prompts as text. TDD producers still update `.active_packet` after an authorized packet write; packet contents are not implementation permission.

### Effort Levels

| Effort | Strategy | Agents |
| :--- | :--- | :--- |
| low | Heuristic: signatures + docstrings | 0 |
| medium | Behavioral Contract (A) + Boundary Exploration (B) | 2 |
| high | A + B + Property-Based Testing (C) | 3 |

### Examples

```bash
/remy-testgen                           # Auto-detect changed files, medium effort
/remy-testgen high src/auth.py          # High effort on specific file
/remy-testgen --tdd                     # TDD mode, detect stubs in changed files
/remy-testgen --tdd task_20260525.json  # TDD mode with remy-plan packet
/remy-testgen low src/utils.py          # Quick heuristic-only generation
```

## Workflow Chain

```
/remy-plan → /remy-testgen --tdd {packet} → /remy-patch {packet} → /remy-inspect
```

## Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `TEST_GEN_EFFORT` | `medium` | Default effort level |
| `TEST_COVERAGE_THRESHOLD` | `80` | Branch coverage target (shared with `/remy-inspect`) |
| `TEST_COVERAGE_MAX_SUPPLEMENT_ROUNDS` | `3` | Max coverage supplement iterations |

## Output

- **Test files**: Written to the project's test directory (auto-detected or user-specified).
- **Report**: `.claude/temp_testgen/testgen_{timestamp}.md`; the actual saved report also records test purpose, requirement sources, failure interpretation and unverified premises as narrative, without changing report JSON.
- **Coverage report** (if supplement declined): `.claude/temp_testgen/coverage_{timestamp}.md`
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
