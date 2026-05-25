# remy-changelog (Change Log Generation)

remy-changelog generates a structured changelog recording modifications, Q&A decisions, and systemic impact. The changelog serves as an audit source for `/remy-audit` and a context preservation mechanism for `/rewind`.

## When to Use

- After each code modification, to record what changed and why
- Before running `/remy-audit`, which requires a changelog as input

## Workflow

### Phase 1: Input Analysis

Reads `git diff --staged` to capture the current changeset (summary and details).

### Phase 2: Context Construction

Builds a context dict matching `output_schema.json`, containing:

- Task ID and status
- Q&A pairs (questions asked and decisions made during the session)
- Per-file modification details (summary, reason, data flow role, ripple effects, line-level logic explanation)
- Systemic impact (data flow, functional hierarchy, framework, API consistency, performance)
- Verification status (tests passed, manual checks)

### Phase 3: Rendering

Uses `render.save_changelog(project_root, context)` to generate the changelog via Jinja2 template (or string formatting fallback) and save it to `.claude/temp_log/`.

## Output Format

Changelogs are saved as `.claude/temp_log/_temp_{task_id}_{timestamp}.md`. Language follows `REMY_LANG`.

## Content Standards

1. **Completeness**: No summarizing or omitting technical details.
2. **Negative knowledge**: Document refuted hypotheses and failed attempts.
3. **Objective style**: Formal indicative sentences. No adjectives, adverbs, or metaphors.
4. **Epistemic humility**: Use "Implemented" or "Attempted" for unverified changes, not "Fixed" or "Solved".

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `output_schema.json` | Context dict structure definition |
| `render.py` | Template rendering helper (Jinja2 with fallback) |
| `templates/changelog.md.j2` | Jinja2 template for changelog output |
