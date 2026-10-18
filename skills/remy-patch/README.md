# remy-patch (Engineered Code Modification)

remy-patch applies code changes with dependency tracing, framework integrity checks and task-scoped strategy. It independently checks relevant definitions rather than treating conversation memory as source evidence. Adaptation follows the selected contract; configuration needs actual variability; an already approved overall change needs no repeated proof that incremental work is impossible.

## When to Use

- After `/remy-plan` approval, to execute planned changes with boundary constraints
- When modifying, refactoring, or optimizing existing code
- When a task packet from `/remy-plan` is available (optional but recommended for complex changes)

## Workflow

### Phase 0: Packet Loading (Conditional)

If a `task_packet_file` argument is provided, the skill reads `.claude/temp_task/{task_packet_file}` and uses `proposed_changes[]` as the authoritative change scope. Changes outside this scope are prohibited. Without a packet, the skill enters free-form discovery mode.

### Phase 1: Dependency Discovery

1. Checks for `.claude/logic_index.db`. If present, runs `impact.py` on target files to produce a bidirectional dependency report (upstream callers and downstream callees).
2. If the logic index is unavailable, falls back to grep/glob-based manual tracing.
3. Reads definitions that can change the implementation, using relevant ranges rather than a fixed all-file depth.
4. Verifies signatures of any external functions to be used.

### Phase 2: Framework Compliance

Checks target files for compiler decorators or metaprogramming patterns that impose language/feature constraints. If detected, verifies the new code is compatible. Skipped if no such patterns are found.

### Phase 3: Execution

For each file to be modified:

1. **Pre-Read & Cache** — Read the file and cache original content for potential rollback.
2. **Discovery Checkpoint** (packet mode only) — Before each Edit call, checks 3 conditions:
   - H1: File not in `proposed_changes[]` (scope overflow)
   - H2: Target function signature changed since audit (stale plan)
   - H3: Edit would violate a constraint in `sender_payload.analysis` (constraint conflict)

   If a condition is true, stop the affected edit. Resolve scope changes with authorization, stale facts with renewed inspection, and requirement conflicts with a decision; there is no blanket ignore option. Preserve original content and owned edits, and check for concurrent changes before any authorized rollback.
3. **Edit & Verify** — Apply the change, use the tool result and an appropriate diff/static check, and record the resulting state; do not re-read solely to prove tool success.
4. **Behavior Decision Log** (packet mode only) — Actual behavior/resource/ordering/boundary choices retain `.claude/temp_decisions/decisions_{PACKET_ID}.md` records and their basis. New user-owned decisions are resolved before implementation; a log does not authorize them. Ordinary syntax choices need no log.

### Phase 4: Validation

Runs relevant tests and the applicable acceptance checks carried in the original plan/analysis fields, including stated manual, compatibility and recovery checks. Each planned check is accounted for as executed, failed or not executed with a reason. Ordinary failures are repaired within approved scope before rerunning; expanded scope, unknown effects, accepting failure or unapproved rollback require a decision.

## Pipeline Integration

This skill is the second stage of the Plan → Modify → Audit pipeline:

```
/remy-plan → task packet → /remy-patch → /remy-audit
```

A task packet adds workflow-specific scope checks. Without one, the skill still follows user authorization and native permissions; packet presence does not authorize execution.

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `../remy-index/impact.py` | Dependency tracing script (invoked in Phase 1) |
