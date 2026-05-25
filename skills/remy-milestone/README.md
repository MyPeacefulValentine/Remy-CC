# remy-milestone (History Reporting)

remy-milestone creates standardized phase reports during the development cycle and maintains a project timeline index. It enforces a "audit first, then document" workflow to ensure technical decisions and experiment results are fully recorded.

## Core Functions

1. **Report Generation**: Captures recent Git changes and produces a structured Markdown report draft.
2. **Timeline Sync**: Extracts the report summary and updates the `.claude/history/timeline.md` index table.
3. **Document Injection**: Updates the timeline reference in `CLAUDE.md` via `hooks/doc_manager/injector.py`.

## Workflow

### Phase 1: Context Saturation (Mandatory)

Before generating any files, the AI must perform a deep audit:

1. Review `git log` to identify all commits since the last milestone.
2. For every modified file, trace its upstream callers and downstream dependencies.
3. Use `Read` to verify the source definitions of changed functions.
4. Do not proceed until "Ripple Effects" and "Systemic Impact" can be explained for every change.

### Phase 2: Generate Draft

Run `/remy-milestone`. The system will:

- Execute `generate_draft.py` to create a timestamped report file in `.claude/history/reports/` (e.g., `20260130_103000.md`).
- Update `.claude/history/timeline.md` with a new row.

### Phase 3: Fill Content

The AI populates the report following the schema in `report_schema.json`. Required sections:

| Section | Content |
| :--- | :--- |
| 1. Summary | Concise summary of work done (1-3 sentences) |
| 2. Technical Decisions | Key architectural choices with rationale |
| 3. Implementation Details | Per-file modification details, data flow role, and ripple effects |
| 4. Systemic Impact Analysis | Impact on data flow, framework, API, performance, concurrency |
| 5. Experiments & Debugging | Test results, logs, or root cause analysis |
| 6. Invariants & PBT Spec | Property-Based Testing invariants |
| 7. Technical Debt & Future Plan | Remaining tasks and known risks |

Language follows the `REMY_LANG` environment variable (`en` or `zh-CN`).

### Phase 4: Summary Sync

After the report is written, run `/remy-milestone` again (or `python "~/.claude/skills/remy-milestone/sync_timeline.py"`). The system will:

- Read the report's Summary section.
- Backfill the summary into `timeline.md`.
- Regenerate `.claude/history/timeline_view.md` according to the current filter configuration.

## Content Standards

1. **Completeness**: Do not summarize or omit technical details to save tokens.
2. **Negative Knowledge**: Document refuted hypotheses and failed attempts.
3. **Objective Style**: Formal indicative sentences. No adjectives, adverbs, or metaphors.
4. **Epistemic Humility**: Do not declare "Fixed" or "Solved" without empirical evidence. Use "Implemented" or "Attempted" for unverified changes.

## Timeline Filter Configuration

The timeline injected into `CLAUDE.md` is sourced from `timeline_view.md`, a filtered view of the full `timeline.md`. Configure via `settings.json` or `settings.local.json`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TIMELINE_INJECT_MODE` | `"all"` | Filter mode |
| `TIMELINE_INJECT_VALUE` | `""` | Mode parameter (see below) |

| Mode | VALUE Meaning | Example |
| :--- | :--- | :--- |
| `all` | Ignored; inject all records | — |
| `last_n` | Integer; keep latest N entries | `"10"` |
| `since_date` | `YYYY-MM-DD`; keep entries after this date | `"2026-03-01"` |
| `within_days` | Integer; keep entries within last N days | `"30"` |

When mode is not `all`, `timeline_view.md` prepends a meta-info line stating the total record count and visible range. The full history is always preserved in `timeline.md`. On invalid `VALUE`, the script falls back to `mode=all` with a stderr warning.

## Directory Structure

```text
.claude/history/
├── timeline.md          # Full index table (Date | ID | Link | Summary)
├── timeline_view.md     # Filtered view (injected into CLAUDE.md)
└── reports/             # Detailed report storage
    ├── 20260124_xxxx.md
    └── 20260130_xxxx.md
```

## Notes

- **No placeholders**: `sync_timeline.py` detects and rejects reports containing `[AI TODO: ...]` placeholders.
- **Idempotent**: Running the generation script multiple times does not overwrite existing files (minute-level timestamp deduplication).

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Full protocol definition (loaded by Claude Code) |
| `report_schema.json` | Report section schema |
| `report_template_en.md` | English report template |
| `report_template_zh.md` | Chinese report template |
| `generate_draft.py` | Draft generation script |
| `sync_timeline.py` | Timeline synchronization script |
