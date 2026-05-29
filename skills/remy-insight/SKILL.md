---
name: remy-insight
description: Deep repository analysis with multi-agent parallel perspectives. Requires /init + /remy-index as prerequisites. Produces structured research reports.
allowed-tools: Read, Grep, Glob, Bash, PowerShell, Write, AskUserQuestion, Agent
argument-hint: "[global | focus <topic> [--with <section>] | compare <doc_path>] [--depth light|standard|deep]"
disable-model-invocation: true
---

# Repository Insight Protocol

Multi-dimensional, multi-agent deep semantic analysis skill. Consumes logic_index.json to analyze repository architecture, identify improvement opportunities, and verify documentation-code consistency.

**Relationship to other skills:**

| Skill | Relationship |
| :--- | :--- |
| remy-index | Data source. Insight consumes logic_index.json |
| remy-reposcout | Upstream. reposcout does shallow recon → user decides to clone → insight does deep research |
| remy-audit | No overlap. audit targets incremental diffs; insight targets whole-repo / focused research |
| remy-secure | Partial overlap (robustness dimension). insight gives high-level observation; secure does rule-driven scanning |

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `INSIGHT_DEFAULT_DEPTH` | `standard` | Depth level when `--depth` is not specified |
| `INSIGHT_MAX_CUSTOM_ANGLES` | `2` | Maximum user-defined custom analysis angles |
| `INSIGHT_MAX_AGENTS` | `30` | Hard cap on total agents per run (analysis + adversarial combined) |

## External Files

> **Path Convention**: All paths below are relative to `~/.claude/`. Use `Read("~/.claude/skills/remy-insight/...")` to access them.

| File | Purpose |
| :--- | :--- |
| `skills/remy-insight/output_schema.json` | Report output JSON Schema |
| `skills/remy-insight/schemas/agent_finding.json` | Single agent output Schema |
| `skills/remy-insight/prompts/angle_architecture.md` | Architecture quality assessment prompt |
| `skills/remy-insight/prompts/angle_improvement.md` | Improvement opportunity identification prompt |
| `skills/remy-insight/prompts/angle_robustness.md` | Security & robustness assessment prompt |
| `skills/remy-insight/prompts/angle_innovation.md` | Technical innovation identification prompt |
| `skills/remy-insight/prompts/angle_custom.md` | User-defined custom angle framework template |
| `skills/remy-insight/templates/_base.md.j2` | Report shell (metadata, section loop, methodology) |
| `skills/remy-insight/templates/section_executive.md.j2` | Executive Summary section |
| `skills/remy-insight/templates/section_architecture.md.j2` | Architecture analysis section |
| `skills/remy-insight/templates/section_innovation.md.j2` | Innovation analysis section |
| `skills/remy-insight/templates/section_improvement.md.j2` | Improvement roadmap section |
| `skills/remy-insight/templates/section_robustness.md.j2` | Robustness analysis section |
| `skills/remy-insight/templates/section_custom.md.j2` | Custom dimension section |
| `skills/remy-insight/render.py` | Template rendering + section assembly + cross-reference annotation |

## Optional Dependency: Jinja2

`render.py` attempts `import jinja2`. If unavailable, all templates are rendered via built-in string formatting. Jinja2 can be installed via `install.py` (optional step).

---

## Phase 0: Input Parsing & Intent Extraction

```
/remy-insight [mode] [args...] [--depth light|standard|deep]
```

### Parsing Rules

1. First argument matches `global` → **global** mode
2. First argument matches `focus` → **focus** mode, second argument is `<topic>`. Optional `--with <section_name>` to append additional sections.
3. First argument is a file path ending in `.md` or `.tex` → **compare** mode
4. First argument matches `compare` → **compare** mode, second argument is `<doc_path>`
5. All other non-empty input → use `AskUserQuestion` to present available modes and let user choose. Do NOT guess intent.
6. No argument → use `AskUserQuestion` to present available modes with descriptions.

### Mode-Section Matrix

| Mode | Default Sections | Description |
| :--- | :--- | :--- |
| `global` | executive, architecture, innovation, improvement, robustness | Full repository analysis |
| `focus <topic>` | executive, architecture, improvement | Focused module/subsystem analysis |
| `compare <doc_path>` | executive, doc_consistency, architecture | Document vs code consistency check |

For `focus` mode, `--with <section>` appends additional sections (e.g., `focus auth --with robustness`).

### Depth Levels

| Depth | Analysis Agents | Multi-Instance | Adversarial Verification |
| :--- | :--- | :--- | :--- |
| `light` | 2 (architecture + improvement only) | No | No |
| `standard` | Per active section count (2-5) | No | Yes — single-refute per issue-severity finding |
| `deep` | Per active section × 2-3 instances | Yes (2-3 per angle) | Yes — 3-vote per concern/issue finding (≥2 consensus adopts) |

**Default depth**: Read `INSIGHT_DEFAULT_DEPTH` from environment. Falls back to `standard`.

---

## Phase 1: Prerequisite Validation

Execute all checks sequentially. HALT on any failure.

### 1.1 CLAUDE.md Check

Run `Bash("test -f CLAUDE.md && echo EXISTS || echo MISSING")` (or PowerShell equivalent on Windows).

- **MISSING**: Print "CLAUDE.md not found. Run `/init` first." → HALT.

### 1.2 Logic Index Check

Run `Bash("test -f .claude/logic_index.json && echo EXISTS || echo MISSING")`.

- **MISSING**: Use `AskUserQuestion`:
  > "`.claude/logic_index.json` does not exist. Run `/remy-index` to generate? This is required for remy-insight."
  - **Yes**: Invoke `remy-index` skill, then continue.
  - **No**: HALT.

### 1.3 Freshness Check (Three-Layer)

**Layer 1 — Dirty file tracker**:
Check if `.claude/logic_index_dirty` exists and is non-empty.
- Non-empty → Flag as `DIRTY`.

**Layer 2 — File set diff**:
`Glob` the current source files matching parser extensions (`.py`, `.c`, `.cpp`, `.h`, `.ts`, `.tsx`). Compare against file paths listed in `.claude/logic_index.json`. Detect additions and deletions.
- Any diff → Flag as `STALE_FILES`.

**Layer 3 — Modification time sampling**:
Read `meta.generated_at` from `logic_index.json`. For up to 10 randomly selected indexed files, compare file modification time (mtime) against `generated_at`. Use seconds-level precision for cross-platform compatibility.
- Any file mtime > generated_at → Flag as `STALE_MTIME`.

**Decision**:
- If any flag is set: Use `AskUserQuestion`:
  > "Logic index may be outdated: {flags}. Update now with `/remy-index`?"
  - **Yes**: Invoke `remy-index`, then continue.
  - **No, continue anyway**: Proceed with warning in report methodology.
  - **Cancel**: HALT.

### 1.4 Compare Mode: Document Validation

Only for `compare` mode.

1. Verify document file exists via `Read`.
2. Verify format: `.md` or single-file `.tex` (Batch 1 scope). Multi-file `.tex` with `\input{}` and `.pdf` are not supported in Batch 1.
3. If unsupported format → Print format limitation → HALT.

**Phase 1 Exit**: All prerequisites satisfied. Proceed to Phase 2.

---

## Phase 2: Scope Alignment & Ambiguity Resolution

Borrowing from remy-plan's loop-until-saturated mechanism.

### 2.1 Focus Mode: Topic Resolution

For `focus` mode, resolve `<topic>` to a file set:

1. Load `.claude/logic_index.json`.
2. Search `<topic>` keywords against:
   - Layer names (exact and substring match)
   - File paths (substring match)
   - Symbol names (substring match)
3. Collect matching files and their direct dependencies (imports/callers at depth 1).
4. If matches = 0: Use `AskUserQuestion` to ask user to specify files or directories.
5. If matches > 30 files: Use `AskUserQuestion` to ask user to narrow scope.
6. Present matched file list to user for confirmation.

### 2.2 Scope Confirmation Loop

```
LOOP:
  1. Present to user:
     - Mode: {mode}
     - Depth: {depth}
     - Active sections: {list with descriptions}
     - Estimated agent count: {N analysis + M adversarial = total}
     - Target files (focus mode): {count} files across {layers} layers
  2. AskUserQuestion: "Proceed with this configuration?"
     - Proceed → Break
     - Modify sections → adjust active sections → re-present
     - Change depth → adjust → re-present
     - Add custom angle → collect user prompt → append → re-present
  3. Check: total estimated agents ≤ INSIGHT_MAX_AGENTS
     - Exceeds → warn user, suggest reducing depth or sections
```

**Phase 2 Exit**: Configuration locked. Proceed to Phase 3.

---

## Phase 3: Multi-Agent Parallel Analysis

### 3.1 Context Preparation

For each active section, prepare the agent's input context:

1. **global mode**: Full logic_index.json content (layer tree + symbols + call graph).
2. **focus mode**: Filtered subset of logic_index.json (matched files + direct dependencies only).
3. **compare mode**: Full logic_index.json + document content from Phase 1.4.

Read the relevant source files for the scope. For files > 500 lines, read only the symbol ranges from logic_index.

### 3.2 Agent Dispatch

**Mode A — Single-instance per angle** (light / standard):

For each active section's corresponding angle prompt:

1. Read the prompt template: `Read("~/.claude/skills/remy-insight/prompts/angle_{section_name}.md")`
2. Read the schema: `Read("~/.claude/skills/remy-insight/schemas/agent_finding.json")`
3. Construct agent prompt:
   - Inject logic_index context
   - Inject relevant source code excerpts
   - Inject mode-specific context (focus target, document content for compare)
   - Append schema requirement
   - Append language instruction: findings must use REMY_LANG for `claim` and `evidence` fields
4. Launch Agent with the constructed prompt.

Launch all angle agents in parallel using the `Agent` tool.

**Mode B — Multi-instance per angle** (deep only):

For each active section, launch 2-3 agents with identical base prompt but different bias tags appended:
- Instance 1: `[BIAS: conservative-assessment]`
- Instance 2: `[BIAS: aggressive-assessment]`
- Instance 3: `[BIAS: devil-advocate]` (if 3 instances)

### 3.3 Agent Failure Handling

Track successful and failed agents.

- Failed agent (timeout, malformed output, schema violation): Mark section as `[skipped: agent error]`.
- If failed count ≥ 50% of active sections: HALT. Report error to user.
- Otherwise: Continue with available results. Record skipped sections in methodology notes.

### 3.4 Adversarial Verification

**Trigger**: Only for `standard` and `deep` depth levels.

**standard mode**:
- For each finding with `severity: "issue"`: Launch 1 adversarial Agent.
- Agent prompt: "Attempt to refute this finding. Provide evidence for or against. Output: `refuted` / `upheld` / `inconclusive`."
- Append `verified_status` field to the finding.

**deep mode**:
- For each finding with `severity: "concern"` or `"issue"`: Launch 3 adversarial Agents.
- Each agent independently attempts to refute.
- Consensus rule: ≥2 agents agree → adopt that verdict. Otherwise → `inconclusive`.
- Append `verified_status` and `vote_detail` fields to the finding.

**Agent cap enforcement**: Before launching adversarial agents, check cumulative agent count against `INSIGHT_MAX_AGENTS`. If launching all would exceed the cap, prioritize `issue`-severity findings over `concern`, and skip remaining.

---

## Phase 6: Report Generation

### 6.1 Cross-Reference Annotation (Pre-Render)

Before rendering, scan all findings across all sections:

1. Build a target index: map `(file, symbol)` → list of finding IDs.
2. For each target that appears in ≥ 2 findings across different sections:
   - Add `cross_refs` field to each finding: list of `"§{section_name} {finding_id}"` strings.

### 6.2 Section Assembly

1. Read `render.py`: `Read("~/.claude/skills/remy-insight/render.py")`
2. For `compare` mode with `doc_consistency` section: inject document claims and verification results.
3. Call render logic (conceptually — actual execution is inline):

```
active_sections = [locked section list from Phase 2]
rendered = []
accumulated = []

for section_name in active_sections:
    context = {
        "findings": findings_by_section[section_name],
        "upstream_findings": accumulated.copy(),
        "metadata": { mode, depth, scope, agent_count, ... },
    }
    content = render_template(f"section_{section_name}.md.j2", context)
    rendered.append({"name": section_name, "rendered_content": content})
    accumulated.extend(findings_by_section[section_name])

report = render_template("_base.md.j2", {
    "active_sections": rendered,
    "active_section_names": [s["name"] for s in rendered],
    **metadata,
})
```

### 6.3 Report Output

1. Ensure directory: `mkdir -p ".claude/temp_insight"`
2. Write report to `.claude/temp_insight/insight_{timestamp}.md`
3. Print report path and executive summary to the user.

**Skill terminates after report output.** Users may continue discussion in the main conversation using the report as context.

---

## Appendix A: Section-Angle Mapping

| Section Name | Angle Prompt File | Description |
| :--- | :--- | :--- |
| `architecture` | `angle_architecture.md` | Module boundaries, coupling, cohesion, dependency direction |
| `innovation` | `angle_innovation.md` | Novel algorithms, design patterns, unique approaches |
| `improvement` | `angle_improvement.md` | Refactoring candidates, missing abstractions, performance bottlenecks |
| `robustness` | `angle_robustness.md` | Error handling gaps, resource leaks, concurrency hazards |
| `doc_consistency` | *(inline in Phase 3)* | Document claims vs code implementation verification |
| `custom` | `angle_custom.md` | User-defined analysis dimension with `{user_focus}` placeholder |

## Appendix B: Batch 2 Placeholders

The following features are planned for Batch 2 and are NOT implemented in this version:

- **Phase 4**: Consensus detection (majority/divergence classification across agents)
- **Phase 5**: Full document-code consistency audit with claim extraction pipeline
- **Phase 7**: Interactive discussion loop (removed — post-report discussion happens in main conversation)
- **compare mode**: `.pdf` support and multi-file `.tex` with `\input{}` recursion
- **consensus_report.json**: Cross-agent consensus/dissent schema
