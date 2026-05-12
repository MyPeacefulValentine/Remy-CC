<p align="center">
  <img src="remy-assets/logo.svg" width="200" alt="Remy">
</p>

<h1 align="center">Remy</h1>

<p align="center">
  A configuration suite for <a href="https://docs.anthropic.com/en/docs/claude-code">Claude Code</a> that adds<br>
  structured workflows, automated context maintenance, and behavioral rules to AI coding sessions.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>&nbsp;
  <img src="https://img.shields.io/badge/Claude_Code-≥2.1.10-blueviolet" alt="Claude Code ≥2.1.10">&nbsp;
  <img src="https://img.shields.io/badge/Python-3.7+-green.svg" alt="Python 3.7+">
</p>

<p align="center">
  <a href="README_zh.md">中文</a>&nbsp;|&nbsp;<b>English</b>
</p>

---

## What is Remy-CC?

In large projects — especially when using less capable models — Claude Code can suffer from **AI hallucination** or **context rot**. Symptoms include modifying code without checking dependencies or function signatures, skipping verification steps, overlooking risks, or referencing content and file paths that no longer exist after a refactor. Across sessions, the problem persists: although Claude Code provides commands like `/compact` to compress context and maintain some continuity, they tend to lose structural details such as function signatures and interfaces, and cannot persistently preserve development records or project architecture. These are not bugs in Claude Code; they are inherent limitations of context-window-based AI interaction.

Remy-CC addresses these limitations by adding a layer of **automated enforcement** and **structured workflows** on top of Claude Code. It uses only Claude Code's native extension points — [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) and [CLAUDE.md](https://docs.anthropic.com/en/docs/claude-code/memory#claudemd-files) — without modifying Claude Code itself. Beyond behavioral constraints, Remy-CC also extracts project file structure, semantic indexes, and call relationships, persistently records development history, and injects them into Claude Code's context to enable continuous context awareness and dependency tracking.

The system is built on three coordinated layers:

- **System prompts** (`CLAUDE.md`, `style.md`, output styles) define engineering principles, communication constraints, and prohibited behaviors. They form the static behavioral baseline, loaded at session start.
- **Runtime hooks** fire automatically on Claude Code events — before every tool call, on every user message, and at session lifecycle boundaries. They re-inject behavioral rules to counteract instruction decay, normalize paths and shell environments, enrich file reads with caller/callee context from the logic index, and keep the project tree snapshot current. Hooks are the continuous enforcement layer: they run without user intervention.
- **Skills** are slash commands (`/deep-plan`, `/code-modification`, `/auditor`, etc.) that you invoke manually to execute structured, multi-step development tasks. Each skill defines its own workflow with explicit inputs, outputs, and stop conditions.

These layers are coupled by design. Hooks maintain the context that skills depend on — file tree, semantic code index, and session history are all updated automatically through lifecycle events. In the other direction, skills produce artifacts (task packets, changelogs, audit reports) that hooks validate at tool-call time. For example, `/deep-plan` writes a task packet that constrains which files `/code-modification` is allowed to edit, and `pre_tool_guard` enforces that boundary on every `Edit` call.

Remy-CC does not pursue full automation or multi-agent orchestration. All code-modification skills require manual invocation and block at key decision points for user confirmation. The rationale: when agents pass summaries between each other, structural details like function signatures and type constraints are easily lost. Keeping the human in the development loop preserves control over change intent and scope at every stage.

**What this gives you:**

- **Instruction persistence** — Behavioral rules are re-injected on every user message, surviving across long conversations instead of silently decaying.
- **Dependency-aware code changes** — A semantic logic index with function-level call graph data (Python AST, C/C++/TypeScript tree-sitter) lets the system trace upstream callers and downstream dependencies before code is modified.
- **Automated context maintenance** — The project file tree, semantic code index, and session history update themselves through lifecycle hooks. `CLAUDE.md` references are kept in sync by the document injector.
- **Composable verification pipeline** — Architecture review → code modification → test verification → changelog → context rewind → three-way auditing, chained through JSON task packets in `.claude/temp_task/`. Each step is independent; use what fits the task complexity.
- **Cross-session memory** — The milestone system writes structured history reports to a timeline index. New sessions load a filtered view, providing continuity without flooding the context window.
- **Environment normalization** — Shell encoding, path formatting, Conda/Mamba activation, and file naming conventions are enforced consistently on every tool call, regardless of platform.

## Features

### Hooks (Automated)

| Hook | Trigger | Function |
| :--- | :--- | :--- |
| Protocol Enforcer | Every user message | Re-injects behavioral rules to counteract instruction decay in long conversations |
| Pre-Tool Guard | Before each tool use | Converts absolute paths to relative; injects Conda/Mamba activation and UTF-8 encoding into shell commands; enforces snake_case file naming |
| Logic Enrichment | Before Read/Grep/Glob | Appends caller/callee relationships and architecture layer for the target file (requires logic index) |
| Lifecycle Manager | Session start/end, pre-compaction | Regenerates the project tree snapshot and language directive |
| Document Injector | On demand | Injects project tree, logic index, and timeline references into `CLAUDE.md` |

### Skills (User-Invoked)

Skills with `disable-model-invocation: true` must be invoked manually. Each enforces a specific workflow with defined inputs, outputs, and stop conditions.

| Command | Purpose | Doc |
| :--- | :--- | :--- |
| `/deep-plan` | Analyze architecture risks and resolve ambiguities before writing code | [📖](skills/deep-plan/README.md) |
| `/code-modification` | Apply code changes with dependency tracing and integrity checks | [📖](skills/code-modification/README.md) |
| `/post-verify` | Discover/create tests, run them, evaluate branch coverage and assertion quality | [📖](skills/post-verify/README.md) |
| `/log-change` | Generate a structured changelog recording modifications and impact | [📖](skills/log-change/README.md) |
| `/auditor` | Verify consistency between plan, changelog, and actual code | [📖](skills/auditor/README.md) |
| `/milestone` | Generate a history report and update the project timeline | [📖](skills/milestone/README.md) |
| `/update-logic-index` | Parse source code to generate semantic summaries and call graph data | [📖](skills/update-logic-index/README.md) |
| `/read-logic-index` | Display the current logic index | [📖](skills/read-logic-index/README.md) |
| `/update-tree` | Regenerate the project directory snapshot | [📖](skills/update-tree/README.md) |
| `/repo-audit` | Inspect a GitHub repository in a sandboxed temporary directory | [📖](skills/repo-audit/README.md) |
| `/receiving-feedback` | Process code review feedback with verification before implementation | [📖](skills/receiving-feedback/README.md) |

Other skills (debugging, TDD, git workflow, etc.) are loaded automatically based on context and require no manual invocation.

#### Plan → Modify → Audit Pipeline

Three skills can be chained via JSON task packets in `.claude/temp_task/`:

```
/deep-plan                          → writes task packet
  └→ /code-modification <packet>    → uses packet as change boundary
        └→ /auditor <log> <packet>  → three-way verification (plan vs. log vs. code)
```

Each step is independent. Skipping `/deep-plan` removes the boundary constraints on `/code-modification` and reduces `/auditor` to a two-way check (log vs. code only).

### CLI & Configuration

After installation, the `remy-cc` command is available system-wide:

| Command | Description |
| :--- | :--- |
| `remy-cc ui` | Open browser-based settings editor for `~/.claude/settings.json` |
| `remy-cc project <path>` | Open project-level settings editor for `<path>/.claude/settings.local.json` |
| `remy-cc update` | Fetch and install the latest version |
| `remy-cc verify` | Check installation integrity |
| `remy-cc version` | Print installed version |

The settings editor provides a bilingual interface (English / 中文) for managing environment variables across 7 groups (LLM API, impact analysis, context injection, timeline, post-verify, system, Claude Code). Project-level settings inherit from global by default; individual parameters can be overridden.

## Installation

### Quick Install

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.ps1 | iex
```

### From Source

```bash
git clone https://github.com/Till-Crazy-Tears-Us-Apart/Remy-CC.git
cd Remy-CC
python install.py                # English (default)
python install.py --lang zh-CN   # Simplified Chinese
```

The installer:
- Copies hooks, skills, output styles, and config files to `~/.claude/`
- Merges hook registrations and environment variables into `~/.claude/settings.json` (existing values are preserved)
- Expands hook paths to absolute paths for the current machine
- Prompts for LLM API configuration (URL, model, API key) used by `/update-logic-index`
- Creates the `remy-cc` CLI command and optionally adds it to system PATH

### Update

```bash
# One-liner
curl -fsSL https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.sh | sh -s -- --update

# From source
python install.py
```

### Verify

```bash
python install.py --verify
# or
remy-cc verify
```

### Uninstall

```bash
# One-liner
curl -fsSL https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.sh | sh -s -- --uninstall

# From source
python install.py --uninstall
```

## Getting Started

1. **Start a Claude Code session** in any project directory. Hooks activate automatically.
2. **Run `/update-logic-index`** to generate the semantic code index for your project (requires LLM API configured during installation).
3. **Use `/deep-plan`** before major changes to review architecture risks.
4. **Use `/milestone`** periodically to record progress into the project timeline.

## Recommended Workflow

A full development cycle follows this sequence. Not every step is required for every change — scale to the task complexity.

1. **`/deep-plan`** — Review architecture risks. Resolve ambiguities. Outputs a task packet. ([doc](skills/deep-plan/README.md))
2. **`/code-modification [packet]`** — Apply changes with dependency tracing. Optionally constrained by the task packet. ([doc](skills/code-modification/README.md))
3. **`/post-verify`** — Run tests, evaluate branch coverage (≥ 80%), audit assertion quality. ([doc](skills/post-verify/README.md))
4. **`/log-change`** — Generate a structured changelog recording what changed and why. ([doc](skills/log-change/README.md))
5. **`/rewind`** — (Claude Code built-in) Restore conversation context to the pre-modification checkpoint, removing implementation bias.
6. **`/auditor [log] [packet]`** — Verify consistency between plan, changelog, and code. ([doc](skills/auditor/README.md))
7. **`git commit`** — Commit the verified changes.
8. **`/milestone`** — Record a history report and update the project timeline. ([doc](skills/milestone/README.md))
9. **`/update-tree`** — Refresh the project tree snapshot if file structure changed. ([doc](skills/update-tree/README.md))

For small, low-risk changes, steps 3–6 can be skipped. 

## Requirements

| Requirement | Purpose |
| :--- | :--- |
| Claude Code CLI ≥ 2.1.10 | Event hooks and skill invocation |
| Python 3.7+ | Hook and installer scripts |
| OpenAI-compatible LLM API | Semantic summarization for `/update-logic-index` |
| Conda or Mamba (optional) | Auto-injected into shell environment when present |
| `gh` CLI (optional) | Required by `/repo-audit` |
| tree-sitter Python packages (optional) | Higher-precision C/C++/TypeScript parsing and call graph extraction |

Language is configurable via the `REMY_LANG` environment variable (`en` or `zh-CN`).

## Directory Structure

```text
.
├── install.py                      # Installer (deploy, uninstall, verify)
├── install.sh                      # One-liner installer for macOS/Linux
├── install.ps1                     # One-liner installer for Windows
├── remy-src/                       # CLI source code
│   ├── cli.py                      # remy-cc command dispatcher
│   ├── config_ui.py                # Browser-based settings editor server
│   └── config_ui.html              # Settings editor frontend
├── remy-assets/
│   └── logo.svg
├── CLAUDE.md                       # AI persona and protocol entry point
├── language.md                     # Language directive (generated by installer)
├── style.md                        # Communication rules and tool constraints
├── tools_ref.md                    # Skill and hook reference index
├── settings.example.json           # Configuration template with hook definitions
├── output-styles/
│   └── system-architect.md         # Output style definition (tone, vocabulary rules)
├── skills/                         # Skill definitions (loaded on demand)
│   ├── deep-plan/                  # Architecture review
│   ├── code-modification/          # Code changes
│   ├── post-verify/                # Test verification
│   ├── log-change/                 # Changelog generation
│   ├── auditor/                    # Consistency audit
│   ├── milestone/                  # History reports
│   ├── update-logic-index/         # Semantic code index
│   ├── read-logic-index/           # Index viewer
│   ├── update-tree/                # Project tree snapshot
│   ├── repo-audit/                 # Repository inspection
│   ├── receiving-feedback/         # Code review handler
│   └── ...                         # Other skills (TDD, debugging, git, etc.)
└── hooks/                          # Automated event handlers
    ├── pre_tool_guard.py           # Path, naming, and environment enforcement
    ├── logic_enrichment_hook.py    # Code relationship context injection
    ├── doc_manager/
    │   └── injector.py             # CLAUDE.md reference injection
    ├── env_system/
    │   ├── enforcer_hook.py        # Behavioral rule injection
    │   ├── reminder_prompt_en.md   # Rule template (English)
    │   └── reminder_prompt_zh.md   # Rule template (Chinese)
    └── tree_system/
        ├── generate_smart_tree.py  # Tree generation logic
        └── lifecycle_hook.py       # Session lifecycle handler
```

## Git Configuration

Add to your project's `.gitignore`:

```gitignore
.claude/
```

## Credits

Parts of the skills in this project were inspired by or ported from **[superpowers](https://github.com/obra/superpowers)** by Jesse Vincent.
