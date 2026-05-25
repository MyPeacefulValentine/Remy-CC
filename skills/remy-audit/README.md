# Auditor (Consistency Verification)

Auditor performs independent consistency verification between the initial plan, change log, and actual code. It operates as an adversarial auditor with zero prior knowledge of the coding session — the change log is its only source of intent.

## When to Use

- Before merging code, to verify that implementations match documented intent
- After `/remy-patch` and `/remy-changelog` have completed
- When a task packet from `/remy-plan` is available for three-way verification

## Workflow

### Phase 1: Input Collection

1. **Change log** (required): Reads the log file specified by the user.
2. **Task packet** (optional): If provided, reads `.claude/temp_task/{task_packet_file}` and extracts the initial plan from `sender_payload.plan`.
3. **Source code**: Reads all files referenced in the change log.

### Phase 2: Verification

Verifies the code against the log across 8 dimensions:

| Dimension | What is checked |
| :--- | :--- |
| Data flow | Flow matches description; no hidden side effects |
| Data structures | Efficient definitions; no risky type conversions |
| Framework integrity | Decorator/middleware state; no global state pollution |
| API consistency | Signatures match documentation; strict parameter types |
| Pipeline impact | No breakage of existing functionality |
| Ripple effects | 1-level deep import/usage check of modified functions |
| Performance & safety | OOM risks, algorithm complexity |
| Test strategy | Critical path coverage; no mock-only testing |

### Phase 3: Output

Generates two tables:

- **Table 1: Intent vs Implementation** — Triangulates initial plan, change log, and actual code. Reports discrepancies.
- **Table 2: Defensive Audit** — Checks side effects, ripple effects, test strategy, and performance safety.

### Phase 4: Stop

The auditor stops and presents options: Fix / Accept / Investigate.

## Verification Modes

| Mode | Condition | Behavior |
| :--- | :--- | :--- |
| Three-way | Task packet provided | Plan vs. log vs. code |
| Two-way | No task packet | Log vs. code only (plan column marked N/A) |

## Prohibitions

- **Read-only**: The auditor cannot modify code.
- **No assumptions**: If a file cannot be read, the auditor reports it rather than guessing.

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `output_schema.json` | Verification depth schema |
