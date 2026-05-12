# Receiving Feedback (Code Review Handler)

Receiving Feedback processes code review comments with technical verification before implementation. It enforces a "verify first, implement second" workflow — feedback is checked against the codebase before any changes are made.

## When to Use

- When receiving code review feedback (from teammates, external reviewers, or PR comments)
- When review comments are unclear or technically questionable
- When multiple review items need prioritized handling

## Workflow

### Phase 1: Read and Understand

Read all feedback items without reacting. Restate each requirement in your own words, or ask for clarification if unclear.

### Phase 2: Verify

Check each suggestion against the codebase:

- Is it technically correct for this project?
- Does it break existing functionality?
- Is there a reason for the current implementation?
- Does it conflict with prior architectural decisions?

### Phase 3: Evaluate and Respond

- If correct: acknowledge factually and proceed to implementation.
- If incorrect or risky: push back with technical reasoning.
- If unclear: stop and ask for clarification before implementing anything.

### Phase 4: Implement

One item at a time, in priority order:
1. Blocking issues (breakage, security)
2. Simple fixes (typos, imports)
3. Complex fixes (refactoring, logic)

Test each fix individually.

## Handling Unclear Feedback

If any item is unclear, stop entirely. Do not implement understood items while waiting for clarification — items may be interdependent.

## When to Push Back

- Suggestion breaks existing functionality
- Reviewer lacks full context
- Violates YAGNI (feature is unused)
- Technically incorrect for the current stack
- Conflicts with prior architectural decisions

## Prohibitions

- No performative agreement ("Great point!", "You're absolutely right!")
- No gratitude expressions ("Thanks for catching that!")
- No blind implementation before verification

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
