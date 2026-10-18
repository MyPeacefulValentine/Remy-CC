# Execution Boundaries and Failure Handling

Shared procedure for `remy-patch`, `remy-debug`, and task reminders. Authorization rules are defined in `style.md`; native Plan and other read-only restrictions take precedence.

## Stop conditions

- Unrecoverable state, suspected data loss, or effects that cannot be established: stop the affected operation and preserve evidence.
- Repair outside the approved scope, an additional deletion, external publication, credentials, or production access: explain the proposed change and request authorization before proceeding.
- Explicit user stop, withdrawn authorization, or changed scope: do not continue or automatically recover the previous execution.
- A task-specific budget is exhausted, or attempts repeat the same action without new evidence: report the unresolved issue and ask for the decision that blocks progress. Do not impose a universal two-failure limit; `remy-debug` retains its configured hypothesis budget.

## Decision boundaries

Apply the code-task planning core in `rules/reasoning-and-decisions.md`. Bounded implementation choices may proceed under applicable requirements and approval. Before implementing a new contract, ownership, migration, external-effect, or acceptance-boundary decision, resolve the required user choice or authorization. Preserve unaffected authorized work when a factual unknown blocks only one part; do not turn an unknown into a default design or stop unrelated work automatically. This does not override a skill's own manifest/scenario budgets, visible checkpoints, or explicit stop obligations.

## User questions

Pause related execution and answer the question first. A question alone does not revoke previously approved scope. Resume only if that scope remains applicable and the user has not requested a stop or changed it. Ask again when continuation requires new authorization, not merely because a question occurred.

## Routine failures within approved scope

Diagnose test, compilation, formatting, and type-check failures from their output. Distinguish implementation, test, and environment defects; repair within the approved scope and rerun affected checks. Do not require a new confirmation for each ordinary failed check. Do not accept a failing check, perform an unapproved rollback, or expand scope without the needed decision.

## Reporting a blocked operation

State the observed failure and its scope, distinguish evidence from inference, explain the available choices and consequences, and ask for the specific unresolved decision or authorization. Never treat an automated notification, an artifact path, or a recovery prompt as approval. Effects that are unknown must not be replayed as if unexecuted.
