# remy-insight

Deep repository analysis with multi-agent parallel perspectives.

## Prerequisites

1. Run `/init` to generate `CLAUDE.md`
2. Run `/remy-index` to generate `logic_index.json`
3. Run `/clear` to refresh injected context

## Usage

```
/remy-insight [mode] [options]
```

### Modes

| Mode | Syntax | Description |
| :--- | :--- | :--- |
| Global | `/remy-insight global` | Full repository analysis across all dimensions |
| Focus | `/remy-insight focus <topic>` | Focused analysis on a specific module/subsystem |
| Compare | `/remy-insight compare <doc_path>` | Document vs code consistency check |

### Options

| Option | Values | Default | Description |
| :--- | :--- | :--- | :--- |
| `--depth` | `light`, `standard`, `deep` | `standard` | Analysis depth level |
| `--with` | section name | — | Append additional sections (focus mode only) |

### Depth Levels

- **light**: 2 agents (architecture + improvement), no adversarial verification
- **standard**: 2-5 agents per active sections, single-refute adversarial for issue-severity findings
- **deep**: 2-3 instances per angle with bias diversity, 3-vote adversarial for concern/issue findings

## Examples

```
/remy-insight global
/remy-insight global --depth deep
/remy-insight focus authentication
/remy-insight focus video-pipeline --with robustness
/remy-insight compare docs/design.md
/remy-insight README.md
```

## Output

Reports are saved to `.claude/temp_insight/insight_{timestamp}.md`.

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `INSIGHT_DEFAULT_DEPTH` | `standard` | Default depth when `--depth` is omitted |
| `INSIGHT_MAX_CUSTOM_ANGLES` | `2` | Maximum user-defined custom analysis angles |
| `INSIGHT_MAX_AGENTS` | `30` | Hard cap on total agents per run |

## Relationship to Other Skills

| Skill | Relationship |
| :--- | :--- |
| `/remy-index` | Data source — insight consumes `logic_index.json` |
| `/remy-reposcout` | Upstream — reposcout does shallow recon, insight does deep research |
| `/remy-secure` | Partial overlap on robustness; insight gives high-level view |

## Batch Status

**Current: Batch 1** — core analysis with cross-reference annotation.

Batch 2 (planned): consensus detection, full document-code audit with claim extraction, `.pdf` support, multi-file `.tex` support.
