# Code Modification (Engineered Change Protocol)

Code Modification applies code changes with dependency tracing, framework integrity checks, and incremental change enforcement. It operates in a "forked context" — the AI independently discovers the call chain and dependencies rather than relying on conversation memory.

## When to Use

- After `/remy-plan` approval, to execute planned changes with boundary constraints
- When modifying, refactoring, or optimizing existing code
- When a task packet from `/remy-plan` is available (optional but recommended for complex changes)

## Workflow

### Phase 0: Packet Loading (Conditional)

If a `task_packet_file` argument is provided, the skill reads `.claude/temp_task/{task_packet_file}` and uses `proposed_changes[]` as the authoritative change scope. Changes outside this scope are prohibited. Without a packet, the skill enters free-form discovery mode.

### Phase 1: Dependency Discovery

1. Checks for `.claude/logic_index.json`. If present, runs `impact.py` on target files to produce a bidirectional dependency report (upstream callers and downstream callees).
2. If the logic index is unavailable, falls back to grep/glob-based manual tracing.
3. Reads all files at Upstream Depth 1 and Downstream Depth 1.
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

   If any condition is true, a **hard interrupt** fires: the AI halts and presents the user with options to expand scope, abort with rollback, or ignore and continue.
3. **Edit & Verify** — Apply the change and verify via post-read.
4. **Soft Decision Log** (packet mode only) — Behavioral choices not covered by the packet are recorded to `.claude/temp_decisions/decisions_{PACKET_ID}.md`. No file is created if no undocumented decisions were made.

### Phase 4: Validation

Runs relevant tests. On failure, presents options to fix, revert, or ignore.

## Pipeline Integration

This skill is the second stage of the Plan → Modify → Audit pipeline:

```
/remy-plan → task packet → /remy-patch → /remy-audit
```

With a task packet, the skill enforces strict change boundaries. Without one, it operates without constraints.

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `../remy-index/impact.py` | Dependency tracing script (invoked in Phase 1) |
