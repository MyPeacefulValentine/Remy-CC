# Code Modification (Engineered Change Protocol)

Code Modification applies code changes with dependency tracing, framework integrity checks, and incremental change enforcement. It operates in a "forked context" — the AI independently discovers the call chain and dependencies rather than relying on conversation memory.

## When to Use

- After `/deep-plan` approval, to execute planned changes with boundary constraints
- When modifying, refactoring, or optimizing existing code
- When a task packet from `/deep-plan` is available (optional but recommended for complex changes)

## Workflow

### Phase 0: Packet Loading (Conditional)

If a `task_packet_file` argument is provided, the skill reads `.claude/temp_task/{task_packet_file}` and uses `proposed_changes[]` as the authoritative change scope. Changes outside this scope are prohibited. Without a packet, the skill enters free-form discovery mode.

### Phase 1: Dependency Discovery

1. Checks for `.claude/logic_index.json`. If present, runs `impact.py` on target files to produce a bidirectional dependency report (upstream callers and downstream callees).
2. If the logic index is unavailable, falls back to grep/glob-based manual tracing.
3. Reads all files at Upstream Depth 1 and Downstream Depth 1.
4. Verifies signatures of any external functions to be used.

### Phase 2: Framework Compliance

Checks JIT/Numba compatibility and numpy/JAX array operation safety for modified code.

### Phase 3: Execution

Pre-read → Edit → Post-read verification for each file.

### Phase 4: Validation

Runs tests specified in the plan.

## Pipeline Integration

This skill is the second stage of the Plan → Modify → Audit pipeline:

```
/deep-plan → task packet → /code-modification → /auditor
```

With a task packet, the skill enforces strict change boundaries. Without one, it operates without constraints.

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `../update-logic-index/impact.py` | Dependency tracing script (invoked in Phase 1) |
