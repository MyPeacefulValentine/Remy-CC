<p align="center">
  <img src="logo.svg" width="200" alt="Remy">
</p>

<h1 align="center">Remy</h1>

<p align="center">
  An engineering configuration suite for <a href="https://code.claude.com">Claude Code</a> —<br>
  enforcing AI behavioral boundaries through hooks, prompt injection, and structured protocols.
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

## Requirements

| Dimension | Requirement | Affected Behavior |
| :--- | :--- | :--- |
| **Software** | Claude Code CLI ≥ 2.1.10 | Event hooks and proactive skill invocation |
| **LLM** | OpenAI-compatible (e.g., GLM, Kimi, Qwen) | Per-call billing, high context limits, complex reasoning |
| **OS** | Windows preferred | Cross-platform path handling and shell syntax |
| **Language** | Python 3.7+ | Runs hook scripts |
| **Runtime** | Mamba / Conda | Auto-injected shell environment |
| **CLI Tool** | gh (GitHub CLI) | Required by repo-audit skill |
| **Interaction** | Configurable (`REMY_LANG`) | Protocol headers and output language controlled by `REMY_LANG` env var (`en` / `zh-CN`) |
| **Encoding** | UTF-8 | Enforced stdin/stdout encoding |
| **Shell** | POSIX Bash | Non-standard syntax restricted |
| **Naming** | snake_case | File naming convention enforced |
| **Paths** | Relative preferred | Absolute paths auto-converted |

## Core Mechanisms

### 1. Interaction Constraints

- **Protocol Injection**: `hooks/env_system/enforcer_hook.py` enforces **System Prompt Refreshing**, countering instruction decay in long contexts.
- **Interrupt-Driven Workflow**: Any user question, conditional statement, or error report is treated as a **STOP** signal. Automatic "whack-a-mole" fixes after errors are strictly forbidden.
- **Anti-Jargon Filter**: A vocabulary table is injected into the prompt, suppressing non-engineering buzzwords at the source.

### 2. Dynamic File Tree &ensp;[📖 Doc](skills/update-tree/README.md)

- **Auto-Maintenance**: Maintains a `.claude/project_tree.md` snapshot via `hooks/tree_system/`.
- **Lifecycle Integration**: Auto-updates on `SessionStart` and `PreCompact` events to keep the AI informed of the latest structure.
- **Auto-Injection**: Injects the project tree into `CLAUDE.md` for structural navigation.
- **Configurable Depth**: Control directory depth and file visibility via `.claude/tree_config` to save context tokens.
    ```text
    # Example .claude/tree_config
    src/core -depth -1 -if_file true  # Deep-index core code with files (-1 = unlimited)
    tests/   -depth 1                 # Shallow structure for tests
    !legacy/                          # Exclude legacy directory
    ```

### 3. Environment & Path Guards

- **Path Normalization**: `hooks/pre_tool_guard.py` intercepts absolute paths and converts them to project-relative paths.
- **Shell Enhancement**: Auto-injects `PYTHONIOENCODING` and Conda/Mamba activation scripts for `Bash` tool calls.
- **Agent Interception**: Intercepts high-latency agents (e.g., `Explore`) and requires user confirmation.

### 4. Context Persistence &ensp;[📖 Doc](skills/milestone/README.md)

- **History Index (Milestone System)**:
    - **Architecture**: Two-layer storage — "Timeline Index + Report Details".
    - **Persistence**: The `/milestone` command generates structured history reports and updates `.claude/history/timeline.md`.
    - **Progressive Disclosure**: `CLAUDE.md` only references the timeline index; the AI reads detailed reports on demand, preserving long-term memory while saving tokens.

### 5. Logic Index &ensp;[📖 Doc](skills/update-logic-index/README.md)

- **Update Mechanism (`/update-logic-index`)**:
    - **Core Function**: Generates architecture-layered semantic summaries with call graph data using source code parsing and LLM inference.
    - **Architecture Layering**: Groups files by architectural layer (API, Service, Data, Utility, etc.) based on directory path patterns. User-customizable via `@layer:` directives in `.claude/logic_index_config`.
    - **Call Graph**: Extracts function-level caller→callee relationships (Python AST, C/C++/TypeScript tree-sitter) and resolves callees to qualified cross-file references via import maps.
    - **Multi-Language**: Python (AST), C/C++ (regex fallback + optional tree-sitter), TypeScript/TSX (regex fallback + optional tree-sitter).
    - **Context Injection**: Auto-injects `.claude/logic_tree.md` into `CLAUDE.md` with layer-grouped output and per-file import annotations.
    - **Passive Enrichment**: A dedicated PreToolUse hook (`logic_enrichment_hook.py`) automatically appends caller/callee/layer context when Claude Code reads project files.
    - **Incremental Updates**: Dependency-aware hashing with **Usage-Aware** filtering — only re-analyzes substantially affected files.
    - **Version-Aware**: Records Git commit hash and timestamp for strict version correspondence.
- **Manual Trigger**: Run `/update-logic-index` to refresh the logic index on demand.
- **Recommended Timing**: Keep the project in a "clean" state (no uncommitted changes) when running.

### 6. Development Workflow

This project enforces a strict **Plan-Act-Verify** loop. The following skills/commands must be **manually invoked** by the user:

1. **Architecture Pre-Review (`/deep-plan`)** &ensp;[📖 Doc](skills/deep-plan/README.md)
    - **Phase**: Plan — before writing any code.
    - **Process**:
        1. **Infrastructure Check**: Detect `.claude/logic_index.json`; if missing, prompt user to run `/update-logic-index` or fall back to manual exploration.
        2. **Context Saturation**: If logic index available, run `impact.py` on target files for bidirectional BFS — upstream (who calls this code) and downstream (what this code calls), with cross-layer warnings; force-read all Upstream Depth 1 and Downstream Depth 1 files. Otherwise, use grep/glob-based exploration.
        3. **Ambiguity Elimination**: Identify decision points → batch questions (`AskUserQuestion`) → re-search on new info (loop).
        4. **Finalize**: Output four core tables:
            - `Ambiguity Elimination Matrix` (decision locks)
            - `PBT Property Specification` (mathematical invariants)
            - `Logic Contract Audit` (data flow & risks)
            - `Physical Change Preview` (file-level operations)
        5. **Evidence Packet**: Write evidence chain, Git commit, and change scope to `.claude/temp_task/task_{TIMESTAMP}.json` (`AgentTaskPacketLite` format); update `.active_packet` pointer; provide entry: `/code-modification task_{TIMESTAMP}.json`.
    - **Function**: "Zero-decision" architecture audit — forces identification of ambiguities and side effects; outputs a persistent task packet consumable by `/code-modification` and `/auditor`.

2. **Code Modification (`/code-modification`)**
    - **Phase**: Act — after architecture approval.
    - **Input**: Optional `task_packet_file` (generated by `/deep-plan`).
    - **Process**:
        - With `task_packet_file`: Reads `.claude/temp_task/{task_packet_file}`, uses `evidence_packet.proposed_changes[]` as authoritative change scope; `status: "suspected"` entries must be re-read and confirmed before use.
        - Without: Clears `.active_packet` and enters discovery phase directly.
        - **Discovery**: If `.claude/logic_index.json` exists, runs `impact.py` for deterministic dependency tracing; otherwise falls back to grep/glob.
    - **Function**: Follows the "Forked Context" pattern, enforcing downstream data-flow adaptation, framework integrity checks, and defensive programming.

3. **Post-Verification (`/post-verify`)**
    - **Phase**: Act — after code modification, before changelog generation.
    - **Input**: Optional `target_files` or `changed_functions`.
    - **Process**:
        1. **Scope Identification**: Extract changeset via `git diff` or user specification.
        2. **Test Discovery**: Detect test frameworks via `frameworks.json`, map existing coverage.
        3. **Test Creation**: Generate temporary tests via Jinja2 templates for uncovered symbols (auto-cleaned after verification).
        4. **Fix Loop**: On failure, perform fault isolation (test defect vs. implementation defect); requires `AskUserQuestion` confirmation before modification.
        5. **Coverage Assessment**: Require ≥ 80% branch coverage for modified functions/classes.
        6. **Assertion Quality Audit**: Scan assertion anti-patterns via `anti_patterns.json`; Critical-level findings block passage.
    - **Function**: Post-implementation test coverage verification; temporary tests cleaned after verification; reports persisted to `.claude/temp_test/`.

4. **Change Logging (`/log-change`)**
    - **Phase**: After each modification.
    - **Function**: Generates atomic changelogs recording Q&A and systemic impact, serving as an audit source.

5. **Context Rewind (`/rewind`)**
    - **Phase**: After generating standardized changelog.
    - **Operation**: Use Claude Code's built-in `/rewind` to restore conversation context to the post-audit / pre-modification checkpoint.
    - **Function**: Ensures the AI retains no memory of the modification process, preventing bias in subsequent interactions.

6. **Triangulation Audit (`/auditor`)**
    - **Phase**: Verify — before code merge.
    - **Input**: Changelog (required) + optional `task_packet_file`.
    - **Modes**:
        - With `task_packet_file`: Full **three-way verification** (initial plan vs. changelog vs. actual code).
        - Without: **Two-way verification** (changelog vs. actual code); "Initial Plan" column marked `N/A`.
    - **Function**: Acts as an "adversarial auditor" — consistency checks without prior context to identify intent-implementation deviations.

7. **Git Commit**

8. **Milestone Report (`/milestone`)**
    - **Phase**: After completing a phase or before `/compact`.
    - **Function**: Generates structured history reports recording technical decisions, experiment results, and open issues; updates `.claude/history/timeline.md`.

9. **Project Tree Update (`/update-tree`)**
    - **Phase**: After file structure changes.
    - **Function**: Refreshes `.claude/project_tree.md` snapshot. Supports configurable scan depth.

#### Plan-Modify-Audit Loop

`/deep-plan`, `/code-modification`, and `/auditor` form a cyclable data pipeline through JSON task packets in `.claude/temp_task/`:

```
/deep-plan
  └─→ Writes .claude/temp_task/task_{TIMESTAMP}.json  (evidence chain + change scope)
  └─→ Updates .claude/temp_task/.active_packet
         └─→ /code-modification task_{TIMESTAMP}.json
               (uses proposed_changes[] as authoritative constraint)
                      └─→ /auditor [log_file] task_{TIMESTAMP}.json
                            (extracts initial plan from sender_payload.plan for three-way verification)
```

Skipping `/deep-plan` means `/code-modification` runs without boundary constraints and `/auditor` degrades to two-way verification (no initial plan column).

## Directory Structure

```text
.
├── install.py                      # Installer (deploy, uninstall, verify)
├── CLAUDE.md                       # System entry — core persona and static protocols
├── language.md                     # Language directive (auto-generated by installer and SessionStart hook)
├── style.md                        # Unified protocol layer (Can/Cannot boundaries & Agent limits)
├── tools_ref.md                    # Tool reference (skill & hook index)
├── settings.example.json           # Config template (with hooks configuration)
├── output-styles/                  # Output style definitions
│   └── system-architect.md         # Engineer role card (tone, anti-patterns, vocabulary)
├── skills/                         # Dynamic skill library (loaded on demand)
│   ├── deep-plan/                  # Deep plan: architecture pre-review protocol
│   ├── code-modification/          # Code modification: engineered change protocol
│   ├── log-change/                 # Change logging: changelog generation
│   ├── post-verify/                # Post-verification: test discovery, coverage, assertion audit
│   ├── auditor/                    # Audit agent: three-way consistency check
│   ├── milestone/                  # Milestone: history records & phase summaries
│   ├── update-tree/                # Tree update: manual snapshot refresh (proactive mode)
│   ├── update-logic-index/         # Logic index: semantic summary generation (Python/C/C++/TS)
│   │   └── impact.py               # Impact radius: bidirectional BFS tracing upstream callers and downstream callees
│   ├── read-logic-index/           # Logic index: semantic summary reader
│   ├── repo-audit/                 # Repo audit: safe clone & structure analysis (sandboxed)
│   └── ...                         # Other engineering skills (TDD, Debugging, FileOps, etc.)
└── hooks/                          # Automated hook system
    ├── doc_manager/                # Document manager
    │   └── injector.py             # CLAUDE.md reference injector
    ├── pre_tool_guard.py           # Pre-tool interceptor (paths, naming, environment)
    ├── logic_enrichment_hook.py    # Logic context enrichment (callers/callees/layer)
    ├── env_system/                 # Constraint enforcement system
    │   ├── enforcer_hook.py        # Protocol injection (UserPromptSubmit)
    │   ├── reminder_prompt_en.md   # Constraint prompt (English)
    │   └── reminder_prompt_zh.md   # Constraint prompt (Chinese)
    └── tree_system/                # Project tree automation
        ├── generate_smart_tree.py  # Core generation logic
        └── lifecycle_hook.py       # Lifecycle integration
```

## Installation

### 1. Install

```bash
git clone https://github.com/Till-Crazy-Tears-Us-Apart/Remy-CC.git
cd Remy-CC
python install.py                # Default: English
python install.py --lang zh-CN   # Simplified Chinese
```

The installer performs the following:
- Copies `hooks/`, `skills/`, `output-styles/`, and config files to `~/.claude/`
- Merges hooks, permissions, and env from `settings.example.json` into `~/.claude/settings.json` (does not overwrite existing values)
- Expands hook paths to absolute paths for the current machine
- Sets `REMY_LANG` in `settings.json` and generates `language.md` based on the `--lang` argument (default: `en`)
- Prompts to configure LLM API for Logic Index (URL, model, API key); optionally tests connectivity
- Detects tree-sitter installation; prompts to install if missing (optional, for high-precision C/C++/TypeScript parsing)

### 2. Verify

```bash
python install.py --verify
```

### 3. Uninstall

```bash
python install.py --uninstall
```

### 4. Git Configuration (Recommended)

Add auto-generated metadata directories to `.gitignore`:

```gitignore
.claude/
```

## Credits

Parts of the skills in this project were inspired by or ported from **[superpowers](https://github.com/obra/superpowers)** by Jesse Vincent (obra).
