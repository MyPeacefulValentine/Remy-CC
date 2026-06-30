---
name: remy-insight
description: Deep repository analysis with multi-agent parallel perspectives. Requires /init + /remy-index as prerequisites. Produces structured research reports.
allowed-tools: Read, Grep, Glob, Bash, PowerShell, Write, AskUserQuestion, Agent
argument-hint: "[global | focus <topic> [--with <section>] | compare <doc_path>] [--depth light|standard|deep]"
disable-model-invocation: true
---

# Repository Insight Protocol

Multi-dimensional, multi-agent deep semantic analysis skill. Consumes logic_index.db (SQLite) to analyze repository architecture, identify improvement opportunities, and verify documentation-code consistency.

**Relationship to other skills:**

| Skill | Relationship |
| :--- | :--- |
| remy-index | Data source. Insight consumes logic_index.db |
| remy-reposcout | Upstream. reposcout does shallow recon → user decides to clone → insight does deep research |
| remy-audit | No overlap. audit targets incremental diffs; insight targets whole-repo / focused research |
| remy-secure | Partial overlap (robustness dimension). insight gives high-level observation; secure does rule-driven scanning |

## 0. Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `INSIGHT_DEFAULT_DEPTH` | `standard` | Depth level when `--depth` is not specified |
| `INSIGHT_MAX_CUSTOM_ANGLES` | `2` | Maximum user-defined custom analysis angles |
| `INSIGHT_MAX_AGENTS` | `40` | Hard cap on total agents per run (analysis + adversarial combined) |

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
| `skills/remy-insight/render.py` | Template rendering + section assembly + cross-reference annotation + CLI entry point |

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

Run `Bash("test -f .claude/logic_index.db && echo EXISTS || echo MISSING")`.

- **MISSING**: Use `AskUserQuestion`:
  > "`.claude/logic_index.db` does not exist. Run `/remy-index` to generate? This is required for remy-insight."
  - **Yes**: Invoke `remy-index` skill, then continue.
  - **No**: HALT.

### 1.3 Freshness Check (Three-Layer)

**Layer 1 — Dirty file tracker**:
Check if `.claude/logic_index_dirty` exists and is non-empty.
- Non-empty → Flag as `DIRTY`.

**Layer 2 — File set diff**:
`Glob` the current source files matching parser extensions (`.py`, `.c`, `.cpp`, `.h`, `.ts`, `.tsx`). Compare against file paths from `SELECT path FROM files` in `.claude/logic_index.db`. Detect additions and deletions.
- Any diff → Flag as `STALE_FILES`.

**Layer 3 — Modification time sampling**:
Query `SELECT value FROM meta WHERE key='last_updated'` from `logic_index.db`. For up to 10 randomly selected indexed files, compare file modification time (mtime) against `last_updated`. Use seconds-level precision for cross-platform compatibility.
- Any file mtime > last_updated → Flag as `STALE_MTIME`.

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

For `focus` mode, resolve `<topic>` to a file set.

**Hierarchical-Summary Path (primary)**: If `remy-index` MCP server is available and the file/cluster summary layer is bootstrapped:

1. Invoke `query_navigate(intent=<topic>, top_k=10)`.
2. Extract the `file?` paths from the ranked entries; if a ranked entry references only a `cluster`, expand it by reading the cluster's files via the SQL detail layer (step 1-2 of the SQL Keyword Path below) restricted to that cluster.
3. Collect the resulting files and their direct dependencies (imports/callers at depth 1 via the `edges` table).
4. If `query_navigate` returns an empty result, an error, or MCP is unavailable, **silently fall back** to the SQL Keyword Path below — do NOT halt or warn the user.

**SQL Keyword Path (fallback)**:

1. Open `.claude/logic_index.db` via SQLite.
2. Search `<topic>` keywords against:
   - Layer names (exact and substring match)
   - File paths (substring match)
   - Symbol names (substring match)
3. Collect matching files and their direct dependencies (imports/callers at depth 1).

**Post-processing (common to both paths)**:

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

For each active section, prepare the agent's input context.

**Hierarchical-Summary Overview (primary)**: If MCP is available, prefix each agent's logic_index context with a cluster-level overview obtained via `query_cluster_summary`. **SQL Detail Layer (always)**: The detailed `files` / `symbols` / `edges` query continues to populate the symbol-level context as before.

1. **global mode**:
   - Overview: Invoke `query_cluster_summary()` (no args) → record all cluster `short` and `full` summaries as the top-level overview text.
   - Detail: Query logic_index.db for full project structure (layer assignments from `files`, symbol signatures from `symbols`, call graph from `edges`). Format as structured text.
2. **focus mode**:
   - Overview: For each cluster containing a topic-matched file (from Phase 2.1), invoke `query_cluster_summary(name=<cluster>)` → record the `short` and `full` summaries.
   - Detail: Query filtered subset from logic_index.db (matched files + direct dependencies via `edges` table).
3. **compare mode**:
   - Overview: Invoke `query_cluster_summary()` (no args) → record all cluster summaries.
   - Detail: Full DB query as in global mode + document content from Phase 1.4.
   - **Prompt Partition Header (MUST)**: In the constructed agent prompt for `compare` mode, the cluster overview text MUST be enclosed under a `[Code-side Cluster Summary]` section header, and the document content MUST be enclosed under a `[Document Excerpt]` section header. Both headers MUST appear exactly once. This prevents the LLM from confusing the doc anchor with the code-side cluster description.

**Fallback**: If MCP is unavailable, or `query_cluster_summary` returns "No clusters found" / an error, silently skip the Overview step for the affected mode. The SQL Detail Layer continues unchanged. For `compare` mode, when the Overview is skipped, the `[Code-side Cluster Summary]` header MUST be omitted (the partition rule applies only when the overview content exists); the `[Document Excerpt]` header MUST still be present.

Read the relevant source files for the scope. For files > 500 lines, read only the symbol ranges from logic_index.

### 3.2 Workflow Dispatch

Phase 3.1 prepares the context variables. Phase 3.2 constructs and invokes a single `Workflow` call that orchestrates all analysis and adversarial agents.

**Pre-dispatch preparation** (executed in main session before Workflow call):

1. Read all angle prompt templates for active sections: `Read("~/.claude/skills/remy-insight/prompts/angle_{section_name}.md")`
2. Read the schema: `Read("~/.claude/skills/remy-insight/schemas/agent_finding.json")`
3. For each active section, construct a full agent prompt string:
   - Inject logic_index context (from Phase 3.1)
   - Inject relevant source code excerpts (from Phase 3.1)
   - Inject mode-specific context (focus target, document content for compare)
   - Append language instruction: findings must use REMY_LANG for `claim` and `evidence` fields
4. Collect all prompt strings into a data structure to be passed as `args` to the Workflow.

**Workflow invocation**: Call `Workflow` with the following script template. Replace `{PLACEHOLDERS}` with the actual values computed above.

```javascript
export const meta = {
  name: 'remy-insight-analysis',
  description: 'Multi-agent repository analysis with adversarial verification',
  phases: [
    { title: 'Analyze', detail: 'Parallel analysis agents per dimension' },
    { title: 'Dedup', detail: 'Deduplicate findings across agents' },
    { title: 'Verify', detail: 'Adversarial verification of concern/issue findings' },
  ],
}

const FINDING_SCHEMA = args.findingSchema
const SECTIONS = args.sections
const DEPTH = args.depth
const MAX_AGENTS = args.maxAgents

phase('Analyze')

const analysisResults = await parallel(
  SECTIONS.map(section => () =>
    agent(section.prompt, {
      label: `analyze:${section.name}`,
      phase: 'Analyze',
      schema: FINDING_SCHEMA,
    })
  )
)

const validResults = analysisResults.filter(Boolean)
const analysisAgentCount = SECTIONS.length

if (validResults.length < Math.ceil(SECTIONS.length / 2)) {
  return { error: true, reason: 'Over 50% of analysis agents failed', validCount: validResults.length, totalCount: SECTIONS.length }
}

const allFindings = []
const summaries = {}
const skippedSections = []

for (let i = 0; i < SECTIONS.length; i++) {
  const result = analysisResults[i]
  const sectionName = SECTIONS[i].name
  if (!result) {
    skippedSections.push({ name: sectionName, reason: 'agent error' })
    continue
  }
  summaries[sectionName] = result.summary
  for (const f of result.findings) {
    allFindings.push(f)
  }
}

phase('Dedup')

const dedupMap = {}
for (const f of allFindings) {
  const key = (f.target.file || '') + '::' + (f.target.symbol || '')
  const severityRank = { issue: 3, concern: 2, observation: 1 }
  const existing = dedupMap[key]
  if (!existing || (severityRank[f.severity] || 0) > (severityRank[existing.severity] || 0)) {
    dedupMap[key] = f
  }
}
const uniqueFindings = Object.values(dedupMap)
const issueFindings = uniqueFindings.filter(f => f.severity === 'issue')
const concernFindings = uniqueFindings.filter(f => f.severity === 'concern')

log(`Dedup: ${allFindings.length} → ${uniqueFindings.length} unique (${issueFindings.length} issue, ${concernFindings.length} concern)`)

if (DEPTH === 'light') {
  for (const f of allFindings) { f.verified_status = 'not_verified' }
  return { findings: allFindings, summaries, skippedSections, analysisAgentCount, adversarialAgentCount: 0 }
}

phase('Verify')

let remainingBudget = MAX_AGENTS - analysisAgentCount
let adversarialAgentCount = 0

const issueVerified = await parallel(
  issueFindings.slice(0, Math.floor(remainingBudget / 3)).map(f => () => {
    const votesNeeded = DEPTH === 'deep' ? 3 : 1
    const votePromises = Array.from({ length: votesNeeded }, (_, vi) => () =>
      agent(
        `Attempt to REFUTE this finding. Provide evidence for or against.\n\nFinding: ${f.claim}\nEvidence: ${f.evidence}\nTarget: ${f.target.file} → ${f.target.symbol || '(module-level)'}\n\nOutput ONLY one of: refuted / upheld / inconclusive`,
        { label: `verify:issue:${f.id}:v${vi}`, phase: 'Verify' }
      )
    )
    return parallel(votePromises).then(votes => {
      const validVotes = votes.filter(Boolean).map(v => {
        const t = (typeof v === 'string' ? v : '').toLowerCase()
        if (t.includes('refuted')) return 'refuted'
        if (t.includes('upheld')) return 'upheld'
        return 'inconclusive'
      })
      const counts = { upheld: 0, refuted: 0, inconclusive: 0 }
      for (const v of validVotes) counts[v]++
      const verdict = counts.upheld >= 2 ? 'upheld' : counts.refuted >= 2 ? 'refuted' : 'inconclusive'
      f.verified_status = verdict
      f.vote_detail = `${counts.upheld} upheld / ${counts.refuted} refuted / ${counts.inconclusive} inconclusive`
      return f
    })
  })
)

adversarialAgentCount += issueFindings.slice(0, Math.floor(remainingBudget / 3)).length * (DEPTH === 'deep' ? 3 : 1)
remainingBudget -= adversarialAgentCount

if (remainingBudget > 0 && concernFindings.length > 0) {
  const concernSlice = concernFindings.slice(0, remainingBudget)
  const concernVerified = await parallel(
    concernSlice.map(f => () =>
      agent(
        `Attempt to REFUTE this finding. Provide evidence for or against.\n\nFinding: ${f.claim}\nEvidence: ${f.evidence}\nTarget: ${f.target.file} → ${f.target.symbol || '(module-level)'}\n\nOutput ONLY one of: refuted / upheld / inconclusive`,
        { label: `verify:concern:${f.id}`, phase: 'Verify' }
      ).then(v => {
        const t = (typeof v === 'string' ? v : '').toLowerCase()
        f.verified_status = t.includes('refuted') ? 'refuted' : t.includes('upheld') ? 'upheld' : 'inconclusive'
        return f
      })
    )
  )
  adversarialAgentCount += concernSlice.length
}

for (const f of allFindings) {
  if (!f.verified_status) f.verified_status = 'not_verified'
}

return { findings: allFindings, summaries, skippedSections, analysisAgentCount, adversarialAgentCount }
```

**Workflow `args` structure** — constructed by the main session and passed verbatim:

```json
{
  "findingSchema": <contents of agent_finding.json>,
  "sections": [
    {"name": "architecture", "prompt": "<full constructed prompt>"},
    {"name": "innovation", "prompt": "<full constructed prompt>"}
  ],
  "depth": "standard",
  "maxAgents": 40
}
```

**Mode dispatch rules** (determine the `sections` array content):

- **Mode A** (light / standard): One entry per active section. Prompt = angle template + context + language instruction.
- **Mode B** (deep): For each active section, 2-3 entries with identical prompt but different bias tags appended (`[BIAS: conservative-assessment]`, `[BIAS: aggressive-assessment]`, `[BIAS: devil-advocate]`).
- **Mode C** (focus × deep): For the `innovation` dimension with sub-angle sets, expand into sub-angle × bias entries. Each entry's prompt has `{sub_angle_instructions}` replaced with the sub-angle focus block.

**Default sub-angle sets** (fixed, not user-configurable):

| Dimension | Sub-Angle | Focus |
| :--- | :--- | :--- |
| `innovation` | `algorithm` | Algorithms, mathematical techniques, complexity analysis, SOTA comparison |
| `innovation` | `architecture` | Non-conventional architectural choices, engineering constraints and trade-offs |
| `innovation` | `limitation` | Technical boundaries, degradation scenarios, unexplored directions |

**Sub-angle injection** (Mode C only):

Replace `{sub_angle_instructions}` in the prompt template with:
```
## Sub-Angle Focus: {sub_angle_name}

For this analysis, focus specifically on: {sub_angle_focus_description}

Prioritize findings within this sub-angle. You may still report findings outside this sub-angle if they are severity "issue", but allocate at least 80% of your findings budget to the specified focus area.
```

Dimensions without sub-angle sets (architecture, improvement, robustness, custom) replace `{sub_angle_instructions}` with an empty string and use Mode B unchanged.

**Agent count**: Total `sections` array length. Check against `INSIGHT_MAX_AGENTS` before invoking Workflow. If exceeded, warn user and suggest reducing depth or sections.

### 3.3 Workflow Failure Handling

If the Workflow call returns an error (script error, timeout, or returns `{error: true}`):

1. **HALT** immediately.
2. Report the error details to the user.
3. Do NOT attempt to fall back to Agent-based dispatch.
4. Print: "Workflow execution failed: {error details}. The Workflow script may need correction, or the context may be too large for the current configuration."

### 3.4 Adversarial Verification

Adversarial verification is embedded within the Workflow script (see Phase 3.2). The Workflow handles it in its `Verify` phase.

**Dedup-then-verify strategy**:

After all analysis agents return, the Workflow deduplicates findings by `target.file + target.symbol` exact match before verification. When multiple findings share the same target, the one with highest severity is retained as the representative. This reduces the verification target count (observed ~50-65% reduction in testing).

**Dynamic budget allocation**:

1. Compute `remaining_budget = INSIGHT_MAX_AGENTS - analysis_agent_count`.
2. Allocate verification agents in priority order:
   - **issue findings** (3-vote in deep, 1-vote in standard) — allocated first, up to budget.
   - **concern findings** (1-vote) — allocated with remaining budget.
3. Findings that cannot be verified due to budget exhaustion are marked `verified_status: "not_verified"`.

**Verification modes by depth**:

| Depth | issue findings | concern findings | observation findings |
| :--- | :--- | :--- | :--- |
| `light` | not_verified | not_verified | not_verified |
| `standard` | 1-vote refute | not_verified | not_verified |
| `deep` | 3-vote consensus (≥2 agrees) | 1-vote refute (budget permitting) | not_verified |

**Consensus rule** (deep mode, issue findings): ≥2 of 3 votes agree → adopt that verdict. Otherwise → `inconclusive`.

---

## Phase 6: Report Generation

### 6.1 Process Workflow Return Value

The Workflow returns a structured JavaScript object. The main session processes it as follows:

1. **Check for error**: If the return value contains `{error: true}`, follow Phase 3.3 (HALT).
2. **Extract fields** from the return value:
   - `findings` — flat array of all findings (with `verified_status` already set by the Workflow)
   - `summaries` — per-section summary texts
   - `skippedSections` — sections that failed during analysis
   - `analysisAgentCount` — number of analysis agents used
   - `adversarialAgentCount` — number of adversarial agents used

### 6.2 Findings JSON Dump

1. Ensure directory: `mkdir -p ".claude/temp_insight"` (or PowerShell equivalent)
2. Assemble and write `.claude/temp_insight/raw_findings_full.json`:

```json
{
  "mode": "<mode>",
  "depth": "<depth>",
  "dimensions": ["<active section names>"],
  "total_findings": <N>,
  "findings_by_severity": {"issue": <n>, "concern": <n>, "observation": <n>},
  "summaries": {"<section_name>": "<summary text>", ...},
  "agent_count": {"analysis": <n>, "adversarial": <n>, "total": <n>},
  "adversarial_results": {"verified_count": <n>, "upheld": <n>, "refuted": <n>, "inconclusive": <n>},
  "scope_description": "<human-readable scope>",
  "freshness_warnings": [],
  "skipped_sections": [],
  "findings": [<flat array from Workflow return>]
}
```

Count `findings_by_severity` and `adversarial_results` by iterating the findings array in the main session. The `timestamp` field is generated in the main session (Workflow scripts cannot call `Date.now()`).

### 6.3 Programmatic Report Rendering

Invoke `render.py` to generate the final Markdown report:

```
python "~/.claude/skills/remy-insight/render.py" \
  --input ".claude/temp_insight/raw_findings_full.json" \
  --output ".claude/temp_insight/insight_{timestamp}.md"
```

`render.py` performs:
1. Reads the flat findings JSON
2. Groups findings by `dimension` field into per-section buckets
3. Annotates cross-references for targets appearing in ≥ 2 sections
4. Renders each section via its Jinja2 template (or fallback string renderer)
5. For the `innovation` section: renders `mechanism` and `significance` fields as expanded blocks
6. Assembles the full report via `_base.md.j2`

**Do NOT hand-assemble the report in the conversation.** Always use `render.py`.

### 6.4 Report Output

1. Print the report file path to the user.
2. Print the executive summary section inline for immediate visibility.

**Skill terminates after report output.** Users may continue discussion in the main conversation using the report as context.

---

## Appendix A: Section-Angle Mapping

| Section Name | Angle Prompt File | Description |
| :--- | :--- | :--- |
| `architecture` | `angle_architecture.md` | Module boundaries, coupling, cohesion, dependency direction |
| `innovation` | `angle_innovation.md` | Novel algorithms, design patterns, unique approaches. Supports `{sub_angle_instructions}` placeholder for focus×deep sub-angle injection |
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
