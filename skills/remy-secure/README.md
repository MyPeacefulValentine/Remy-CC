# remy-secure

Security-focused code review skill for branch changes. Identifies high-confidence exploitable vulnerabilities through a multi-stage pipeline: deterministic regex pre-scan, parallel category agents, and independent false-positive filtering.

## Usage

```
/remy-secure [low|medium|high] [diff_range]
```

### Examples

```bash
/remy-secure                    # medium effort, origin/HEAD...HEAD
/remy-secure high               # high effort, origin/HEAD...HEAD
/remy-secure medium HEAD~5...HEAD  # medium effort, last 5 commits
```

## Effort Levels

| Level | Phase 0 (Regex) | Category Agents | Filter Agents | Use Case |
| :--- | :--- | :--- | :--- | :--- |
| low | Yes | 0 | 0 | Quick scan for deterministic patterns only |
| medium | Yes | 3 | Up to 15 | Standard PR review |
| high | Yes | 5 | Up to 15 | Pre-release or high-risk change review |

## Architecture

```
Phase 0: Git Discovery → Diff Extraction → Regex Pre-Scan
                                              ↓
Phase 1: Category Agents (parallel)     [medium/high only]
          ├── A: Injection (SQL/Cmd/Path/Template/NoSQL/XXE)
          ├── B: Auth & Authorization
          ├── C: Data Exposure
          ├── D: Crypto & Secrets           [high only]
          └── E: Deserialization/Exec       [high only]
                                              ↓
Phase 2: False-Positive Filter (parallel) [medium/high only]
          └── One agent per finding (max 15)
                                              ↓
Phase 3: Threshold Cutoff (≥ 8/10) → Report Generation
```

## Integration with logic_index

When `.claude/logic_index.json` exists in the project, the skill runs `impact.py` to obtain caller/callee relationships. This data is injected into category agent prompts to enable cross-file data flow tracing (source → sink analysis).

If the logic index is unavailable, the skill degrades gracefully to diff-only analysis.

## Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `SECURITY_AUDIT_EFFORT` | `medium` | Default effort level |
| `SECURITY_AUDIT_MAX_FILTER_AGENTS` | `15` | Max parallel filter agents |
| `SECURITY_AUDIT_CONFIDENCE_THRESHOLD` | `8` | Minimum confidence for final report |

## Customization

- `rules/exclusions.json`: Add/disable hard exclusion rules
- `rules/precedents.json`: Add contextual precedent judgments
- `rules/patterns.json`: Add deterministic regex patterns for Phase 0

## Report Output

Reports are saved to `.claude/temp_test/security_audit_{timestamp}.md` in the project directory.
