# Halt Protocol — Shared Rules (Owner)

Single authoritative definition of the halt semantics shared by every consumer below. Consumer protocols carry a self-sufficient compact summary of these rules plus a reference to this file; when a summary and this file diverge, this file wins.

**Consumers**: `output-styles/system-architect.md` §3.3 (Error Handling) — behavioral layer; `remy-debug` (Phase 2.4 Circuit Breaker), `remy-patch` (Phase 4.3 Failure Handling) — skill protocols; `hooks/env_system/reminder_prompt_zh/en.md` warning line — anti-decay layer, kept as compact repetition by design.

## Trigger Classes (MUST halt)

1. **Unrecoverable error**: evidence of file corruption, data loss, or a state that cannot be restored by re-editing (e.g., an overwritten un-backed-up file).
2. **Out-of-scope repair**: the fix requires deleting files, running destructive operations, expanding beyond the approved scope, or touching credentials, production systems, or other security boundaries.
3. **User interrupt (STOP)**: the user asks a question, discusses logic, or reports an error while modifications are in flight — stop editing, answer first, re-acquire permission.
4. **Retry budget exhausted**: a repair attempt for the same failure has failed 2 times (`remy-patch` 4.3), or a hypothesis loop has hit its iteration cap (`remy-debug` `DEBUG_MAX_HYPOTHESES`).

## Post-Halt Action Sequence (MUST, in order)

Acknowledge (state what failed, verbatim evidence) → Analyze (mechanism, not just symptom) → Propose (options with trade-offs) → Ask Permission (`AskUserQuestion`; never resume on assumed approval).

## Autonomous-Repair Boundary (MUST NOT halt)

Routine recoverable failures during an authorized implementation — test failures, compile errors, lint/format findings, type errors — are part of the verification loop: diagnose from the error evidence, distinguish implementation defect from test defect from environment issue, apply the minimal fix within approved scope, and re-run the failed check. Halting on every routine failure is a protocol violation; halt only when a trigger class above applies.
