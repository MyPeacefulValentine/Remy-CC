---
name: remy-milestone
description: Record technical decisions and progress as a persistent milestone report. Use before /compact or at task completion.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
disable-model-invocation: true
---

# Milestone Reporting Skill

## Overview
This skill defines the protocol for documenting project milestones. It ensures that technical decisions, experimental results, and architectural changes are preserved beyond the short-term memory of a single session.

## When to Use
- **BEFORE /compact**: You MUST run this skill manually before compacting context.
- **Milestone Reached**: Use when you have finished a meaningful piece of work (feature, fix, refactor).

## Workflow

### Phase 1: Context Saturation (Mandatory)
Before generating any files, you MUST perform a deep audit of the work done.

1.  **Identify Scope**:
    *   Review `git log` to identify all commits since the last milestone.
    *   If no commits exist, include staged changes (`git diff --staged`).
2.  **Recursive Audit (Anti-Hallucination)**:
    *   **Trace**: For every modified file, identify its upstream callers and downstream dependencies.
    *   **Read**: Use `Read` to verify the *source definitions* of changed functions.
    *   **Verify**: Confirm you understand the *exact* technical logic, not just the intent.
    *   **Constraint**: Do NOT proceed until you can explain the "Ripple Effects" and "Systemic Impact" for every change.

### Phase 2: Documentation Generation
1.  **Generate Draft**: Use `python "~/.claude/skills/remy-milestone/generate_draft.py"` to create a draft file and update the timeline.
2.  **Fill Content**: Immediately read the generated draft and populate it using the `Write` tool.
    *   **Data Source**: Use the knowledge gathered in Phase 1.
    *   **Language Mandate**: All content MUST follow the loaded `language.md` directive.
3.  **Validation**: Verify the file is saved and the content matches the schema.

### Phase 3: Finalization (Summary Sync)
After the report is written and verified, you MUST synchronize the summary back to the timeline index.

1.  **Sync**: Run `python "~/.claude/skills/remy-milestone/sync_timeline.py"`.
2.  **Verify**: Check that `.claude/history/timeline.md` now contains the meaningful summary from your report. The script also automatically regenerates `.claude/history/timeline_view.md` according to the current `TIMELINE_INJECT_MODE` configuration.

## Content Standards (Strict)
You MUST adhere to the following 4 rules when writing the report:

1.  **Completeness (No Token Saving)**: DO NOT summarize, compress, or omit technical details to save space/tokens. You MUST preserve the FULL technical context, reasoning, and trade-offs.
2.  **Negative Knowledge (Falsification)**: You MUST document *refuted* hypotheses and *failed* attempts. Explaining "why approach X failed" is as critical as "how approach Y succeeded".
3.  **Style (Objective)**: Use formal, simple indicative sentences (`Subject` + `Verb` + `Object`). STRICTLY PROHIBIT unnecessary `Adjectives`, `Adverbs`, and `Metaphors` (e.g., "elegant solution", "quick fix").
4.  **Epistemic Humility**: DO NOT declare "Fixed" or "Solved" without empirical data (logs/tests). Use "Implemented" or "Attempted" for unverified changes. Do not predict future success.

## Core Pattern: The Milestone Report
A milestone consists of two parts:
1.  **Index (Timeline)**: A high-level reverse-chronological list in `.claude/history/timeline.md`.
2.  **Report**: A detailed markdown file in `.claude/history/reports/YYYYMMDD_HHMMSS.md`.

## Report Schema (Strict Compliance)
You must follow the schema defined in `~/.claude/skills/remy-milestone/report_schema.json`.
The generator script handles the skeleton; your job is to populate the content in the language configured by `REMY_LANG`.

## Timeline Filter Configuration

The timeline injected into `CLAUDE.md` is sourced from `.claude/history/timeline_view.md`, a filtered view generated from the full `timeline.md`. Configure the following variables in the `env` block of `.claude/settings.local.json`:

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `TIMELINE_INJECT_MODE` | `"all"` | 过滤模式 |
| `TIMELINE_INJECT_VALUE` | `""` | 模式参数值（见下表） |

| 模式 | VALUE 含义 | VALUE 示例 |
| :--- | :--- | :--- |
| `all` | 忽略，注入全部记录 | — |
| `last_n` | 整数，保留最新 N 条 | `"10"` |
| `since_date` | `YYYY-MM-DD`，保留该日期之后的记录 | `"2026-03-01"` |
| `within_days` | 整数，保留最近 N 天内的记录 | `"30"` |

When mode is not `all`, `timeline_view.md` prepends a meta-info line stating the total record count and the visible range. The full history is always preserved in `timeline.md`. On invalid `VALUE`, the script falls back to `mode=all` and prints a warning to stderr.

## Discovery
The history is indexed in `.claude/history/timeline.md`. Claude will refer to this index in `CLAUDE.md` to progressively discover past reports when needed.
