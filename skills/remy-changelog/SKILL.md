---
name: remy-changelog
description: Generate a temporary change log recording code modifications. Paired with /remy-audit: generate log, rewind AI context (preserving code changes), then run blind audit.
argument-hint: "[task_id] [status]"
allowed-tools: Bash, Read, Write
disable-model-invocation: true
---

# Change Log Generation Protocol

You must generate a change log file at `.claude/temp_log/_temp_{task_id}_{timestamp}.md`.
All content MUST be in the language specified by the `REMY_LANG` environment variable (`zh-CN` → Chinese, `en` → English).

## External Files

> **Path Convention**: All paths below are relative to `~/.claude/`. Use `Read("~/.claude/skills/remy-changelog/...")` to access them — they are NOT in the project working directory.

| File | Purpose |
| :--- | :--- |
| `skills/remy-changelog/output_schema.json` | Context dict structure definition. Populate all required fields. |
| `skills/remy-changelog/render.py` | Template rendering helper. Uses Jinja2 when available, falls back to `str` formatting. |
| `skills/remy-changelog/templates/changelog.md.j2` | Jinja2 template for the change log output. |

## Optional Dependency: Jinja2

`render.py` attempts `import jinja2`. If unavailable, all templates are rendered via built-in string formatting (functionally equivalent, but templates are not externally editable in fallback mode). Jinja2 can be installed via `install.py` (optional step).

## 1. Input Analysis
- **Task ID**: $1
- **Status**: $2
- **Git State**: !`git diff --staged --stat` (summary) AND !`git diff --staged` (details)

## 2. Context Dict Construction

Build a context dict matching `output_schema.json`. The dict MUST contain:

```python
{
    "task_id": "...",
    "status": "...",
    "date": "YYYY-MM-DD",
    "qa_pairs": [
        {"question": "...", "answer": "...", "decision": "..."}
    ],
    "file_modifications": [
        {
            "file_path": "...",
            "summary": "...",
            "reason": "...",
            "role": "...",
            "ripple_effects": ["..."],
            "logic_explanation": "L42-L58: ..."
        }
    ],
    "systemic_impact": {
        "data_flow": "...",
        "functional_hierarchy": "...",
        "framework_impact": "...",
        "api_consistency": "...",
        "performance": "..."
    },
    "verification_status": {
        "tests_passed": ["..."],
        "manual_checks": ["..."]
    }
}
```

## 3. Report Generation

Use `render.save_changelog(project_root, context)` to generate and persist the change log to `.claude/temp_log/`.

## 4. Content Standards (Strict)
You MUST adhere to the following 4 rules when writing the log:

1.  **Completeness (No Token Saving)**: DO NOT summarize, compress, or omit technical details to save space/tokens. You MUST preserve the FULL technical context.
2.  **Negative Knowledge (Falsification)**: You MUST document *refuted* hypotheses and *failed* attempts. Explaining "why approach X failed" is as critical as "how approach Y succeeded".
3.  **Style (Objective)**: Use formal, simple indicative sentences (`Subject` + `Verb` + `Object`). STRICTLY PROHIBIT unnecessary `Adjectives`, `Adverbs`, and `Metaphors`.
4.  **Epistemic Humility**: DO NOT declare "Fixed" or "Solved" without empirical data (logs/tests). Use "Implemented" or "Attempted" for unverified changes.

## 5. Execution Rule
- **No Adjectives**: Use "Implemented X", not "Successfully implemented X".
- **No Innocence Presumption**: Document risks even if tests passed.

## 6. Strict Schema Compliance (Implicit)
You MUST read `~/.claude/skills/remy-changelog/output_schema.json` to understand the required verification depth.
**Do NOT output the JSON block.** Populate the context dict to satisfy the schema's rigor.
