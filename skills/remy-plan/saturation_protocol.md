# Task-Scoped Context Investigation

Shared procedure for `remy-plan`, `remy-patch`, and `remy-milestone`. The `CLAUDE.md` entry summarizes the principle; this file supplies workflow details. Native mode and access restrictions still apply.

## Investigation boundary

1. Inspect source definitions instead of inferring implementation from usage. Follow inheritance, interfaces, callers, and dependencies when they could change the current decision.
2. Identify the behavior, state, and assumptions affected by the task. Do not recursively inspect every dependency or claim that no ambiguous types remain.
3. Stop expanding once the decision-relevant behavior is understood. If an unresolved question could change the solution, investigate it or report the missing evidence before execution. If it cannot change this scope, record the limitation rather than expanding without a purpose.
4. Inspect an agent's cited source before relying on it for a change or verified conclusion. Report the scope of negative searches.

## Requirements and decision dependencies

Use the shared code-task planning core in `rules/reasoning-and-decisions.md`. First identify the goal, preservation requirements, permitted evolution, and actual responsibility owners. Follow the definitions, consumers, and state relations that can change a choice. Separate settled mechanical propagation from choices that constrain another owner's behavior or feasible options; file count alone is not a redesign trigger.

Investigation scope does not grant modification scope. Record unspecified requirements, factual unknowns, and user decisions separately. A blocking unknown must name the affected decision and next observation or scope decision. A nonblocking unknown remains unknown, not an implicit permission or a reason to inspect unrelated dependencies.

## Reading source

Use available line ranges and source definitions to read the necessary context. For files larger than `PRECISION_READ_THRESHOLD` (default 500 lines), prefer bounded ranges; combine overlapping or nearby ranges. Read the whole file when the task requires it, not simply because a range is missing. Inspect surrounding definitions when an excerpt leaves a decision-relevant ambiguity.

Temporary experiments remain subject to authorization and native mode. Writing a probe in TEMP does not make it a read-only action. Do not execute experiments in native Plan mode.
