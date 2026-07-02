# remy-debug (Bug Diagnosis)

remy-debug traces root cause of bugs through a structured hypothesis loop with a circuit breaker. It produces a diagnosis report and an evidence packet compatible with `/remy-patch`. It does NOT modify source code.

## When to Use

- A test is failing and the cause is not obvious
- An error appears in logs or runtime output
- A regression was introduced after recent changes

## Workflow

### Phase 0: Symptom Capture

Collects the observable error — from arguments, test output, or guided questions.

### Phase 1: Localization

Parses stack traces or greps error keywords to identify suspect files. Runs impact analysis via logic index (when available), collects git history for regression scoping, then reads the suspect code.

### Phase 2: Hypothesis Loop

1. Form a hypothesis based on evidence
2. Design a non-invasive probe (read-only Bash, grep, targeted Read)
3. Execute probe and record result
4. Confirm or refute hypothesis
5. Repeat until root cause is identified or circuit breaker triggers (`DEBUG_MAX_HYPOTHESES`, default: 3)

### Phase 3: Diagnosis Report

Outputs a structured report to `.claude/temp_debug/debug_{timestamp}.md` containing:
- Symptom description
- Hypothesis chain with evidence
- Root cause conclusion
- Suggested fix (without implementing it)

### Phase 4: Evidence Packet

Generates a `.claude/temp_task/task_*.json` packet for `/remy-patch` to execute the fix.

### Phase 5: Stop Protocol

After the report and packet are written, prints the diagnosis summary and the follow-up command (`/remy-patch debug_{TIMESTAMP}.json`, only when mode = "write"), then STOPS. Does NOT apply any code changes.

## Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `DEBUG_MAX_HYPOTHESES` | `3` | Maximum hypothesis iterations before circuit breaker triggers. |

## Arguments

```
/remy-debug [error_description | test_command | file:line] [--since <ref>]
```

- No argument: enters guided mode (interactive symptom collection)
- Test file pattern (`test_*.py`): treated as test command
- `file:line` pattern: treated as location reference
- Other text: treated as error description
- `--since <ref>`: scopes regression analysis to changes after the given git ref
