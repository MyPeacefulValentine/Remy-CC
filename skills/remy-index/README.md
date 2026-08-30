# remy-index (Semantic Code Index)

remy-index is a semantic indexing tool based on multi-language source code parsing and an OpenAI-compatible API. It parses Python, C, C++, and TypeScript/TSX code to generate architecture-layered semantic summaries with call graph data, enabling Claude Code to understand project structure and function relationships without reading full source files.

## Architecture Overview

The system operates in two stages across four layers:

```
Stage 1: Structural Scanning (no LLM, Hook-driven)
┌──────────────────────────────────────────────────────────┐
│  struct_scan.py (stable CLI/import entry point)           │
│  ├── schema.py: current SQLite schema contract            │
│  ├── symbol_names.py: shared name tokenization            │
│  ├── migrations.py: initialization + migration ladder     │
│  └── scanner.py: extraction, graph post-pass, full/delta  │
│  Triggers: SessionStart, PreCompact (full scan)           │
│            PreToolUse via dirty file consumer             │
└──────────────────────────────────────────────────────────┘

Stage 2: LLM Summarization (API-dependent, manual invocation)
┌──────────────────────────────────────────────────────────┐
│  run.py (LLM indexer)                                    │
│  ├── Delegates Stage 1 to struct_scan.py                 │
│  ├── Generates semantic summaries for dirty symbols      │
│  └── Saves structural facts and summaries to logic_index.db│
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  logic_index.db (injected into CLAUDE.md)                 │
│  ├── Architecture layer grouping (files grouped by layer)│
│  ├── File-level summaries + imports annotations          │
│  └── Symbol-level signatures + summaries                 │
│  Purpose: Baseline cognition (structure, roles, deps)    │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  logic_index.db (disk cache, not injected)             │
│  ├── Symbol hashes + summary cache                       │
│  ├── struct_hash (per-file raw source fingerprint)       │
│  ├── end_lineno (symbol end line for precision Read)     │
│  ├── File-level imports list                             │
│  ├── File layer assignment                               │
│  └── Function-level CALLS edges (with callee resolution) │
│  Purpose: Hook query source + incremental build cache    │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Hooks (automated pipeline)                              │
│  ├── PostToolUse: dirty file tracker records Edit/Write  │
│  ├── PreToolUse: enrichment hook consumes dirty files,   │
│  │   triggers incremental struct_scan, appends           │
│  │   callers/callees/layer + [L{start}-L{end}] ranges    │
│  └── Lifecycle: full struct_scan on SessionStart/PreCompact
│  Purpose: Continuous structural accuracy without manual  │
│           invocation                                     │
└──────────────────────────────────────────────────────────┘
```

The structural and semantic writers share a project-level process lock. `struct_scan.py` preserves the existing CLI and Python imports; `schema.py`, `symbol_names.py`, `migrations.py`, and `scanner.py` provide the internal implementation. The installer deploys the complete `skills/remy-index/` directory, so these modules are installed together. Structural scans return `success`, `partial`, or `failed` and map these states to exit codes `0`, `2`, and `1`. Dirty paths move through a crash-recoverable processing snapshot; only paths covered by a successful structural scan and global post-pass are acknowledged.

## Supported Languages

| Language | Extensions | Parsing Method | Call Graph |
| :--- | :--- | :--- | :--- |
| Python | `.py` | Standard library `ast` module (built-in) | Supported (AST) |
| C | `.c`, `.h` | Regex (built-in) / tree-sitter (optional) | tree-sitter only |
| C++ | `.cpp`, `.hpp`, `.cc`, `.cxx`, `.hh`, `.hxx` | Regex (built-in) / tree-sitter (optional) | tree-sitter only |
| TypeScript | `.ts`, `.tsx` | Regex (built-in) / tree-sitter (optional) | tree-sitter only |

`.h` files are auto-detected: if they contain C++ keywords (`class`, `namespace`, `template`, etc.), C++ parsing is used.

## Core Features

### Architecture Layering

Files are grouped into architectural layers based on directory path patterns. Default layers:

| Layer | Matching Patterns |
| :--- | :--- |
| API Layer | routes, controller, handler, endpoint, api |
| Service Layer | service, usecase, use-case, business |
| Data Layer | model, entity, schema, database, db, migration, repository, repo |
| UI Layer | component, view, page, screen, layout, widget, ui |
| Middleware Layer | middleware, interceptor, guard, filter, pipe |
| External Services | client, integration, external, sdk, vendor, adapter |
| Background Tasks | worker, job, queue, cron, consumer, processor, scheduler, background |
| Utility Layer | util, helper, lib, common, shared |
| Test Layer | test, spec, __test__, __spec__, __tests__, __specs__ |
| Configuration Layer | config, setting, env |
| Core | (unmatched files) |

Matching rule: file path split by `/` into directory segments, compared case-insensitively against patterns (plural forms auto-matched with `+s`). First-match-wins.

Layer definitions are user-customizable in `.claude/logic_index_config` using `@layer:Name=pattern1,pattern2,...` syntax.

### Call Graph Extraction

Extracts caller-to-callee relationships within each file:

- **Python**: Uses stdlib `ast` module with a function stack pattern. Handles `ast.Name` (simple calls) and `ast.Attribute` (method calls).
- **C/C++/TypeScript**: Uses tree-sitter (when available) with the same function stack pattern. Regex mode returns empty (call extraction requires AST precision).

After extraction, `_resolve_call_edges` resolves callee names to qualified references (e.g., `models/user.py::User.verify_password`) using the file's import list and cached symbol data from target files.

### Passive Enrichment Hook

`remy-cc hook enrich` is a PreToolUse hook command triggered on Read/Glob/Grep operations (registered by the installer; the python hook scripts are retired). It queries `logic_index.db` for the target file directly — prefixed with an index-freshness line when the daemon reports pending or running scan jobs — and outputs:

```
[Logic Context] services/auth.py (Service Layer)
  Calls into: models/user.py::User.verify_password [L42-L68], utils/token.py::generate_jwt [L15-L30]
  Called by: routes/login.py::handle_login, routes/register.py::handle_register
```

The `[L{start}-L{end}]` line ranges enable `remy-plan` and `remy-patch` Skills to use offset-based `Read()` for files exceeding `PRECISION_READ_THRESHOLD` (default: 500 lines), avoiding full-file reads of large files.

This provides relationship context without requiring Claude Code to proactively call MCP tools.

### Multi-Language Parsing

- **AST Parsing (Python)**: Identifies Class, Function, and Method structures.
- **Regex + tree-sitter Dual Path (C/C++/TypeScript)**: Zero-dependency regex mode by default; automatically switches to high-precision mode when tree-sitter is installed.

### Cross-File Context

- Parses Python `import`, C/C++ `#include "..."`, and TypeScript relative `import` dependencies.
- Injects upstream module summaries into LLM prompts for context-aware summarization.
- Displays import list per file in `logic_index.db` output.

### Incremental Updates

- **File-Level Hashing**: MD5-based source content hashing.
- **Structural Hashing (`struct_hash`)**: Raw source MD5 (independent of the symbol-level `hash`). Any byte change — including whitespace or comment edits — triggers structural re-parsing to refresh line numbers and call edges. Unchanged files are skipped entirely.
- **Comment-Insensitive Symbol Hashing**: When checking whether a function's LLM summary needs regeneration, comments (`#`, `//`, `/* */`) are stripped from the source before hashing. This means reformatting comments or adding inline notes does not trigger unnecessary API calls. Docstrings and Doxygen comments are preserved in the hash (they affect summary content). If comment stripping fails, the system falls back to hashing the full source.
- **Dependency-Aware Hashing**: Upstream summary changes trigger downstream re-analysis.
- **Usage-Aware Filtering**: Only triggers updates when referenced symbols are actually used in the current file.

### Hybrid Summary Strategy

- **Docstring/Doxygen Priority**: Auto-extracts Python docstrings and C/C++ Doxygen comments (`[Doc]` tag), zero API cost.
- **Short Function Skip**: Functions under 3 lines without documentation are auto-tagged (configurable).
- **LLM Semantic Enhancement**: Only invokes the LLM API for complex logic.
- **Data Flow Tracking**: Forces LLM to identify data sources `[Source]` and data sinks `[Sink]`.

### Robustness

- **Atomic Fallback**: Batch processing failure triggers automatic degradation to single-symbol mode.
- **Truncation Recovery**: Detects API response truncation and triggers automatic retry.
- Built-in exponential backoff, circuit breaker (auto-stop on 429/401), and checkpoint protection.

## Workflow (4 Phases)

### Phase 1: Check Configuration

The skill checks for `.claude/logic_index_config`. If missing, it creates one from the default template (including layer definitions and exclusion rules) and prompts the user to review before proceeding.

### Phase 2: Execute Scanning

Runs the Python indexer:

```bash
python "~/.claude/skills/remy-index/run.py"
```

On first run (no existing `.claude/logic_index.db`), a full codebase scan is performed. The indexer:
1. Walks the project tree, parses symbols and call graphs per file
2. Resolves callee names to qualified references using import maps
3. Generates LLM summaries for symbols without documentation
4. Saves results to `.claude/logic_index.db` (cache) and `.claude/logic_index.db` (output)

### Phase 2.5: Symbol Summary Confirmation (Conditional)

If `run.py` stdout contains `SYMBOL_PENDING_CONFIRMATION pending_symbols=N`, the skill asks whether to generate symbol summaries via a full rerun with `--symbol-mode auto`. Triggered only when the effective `REMY_SYMBOL_SUMMARY_MODE` is `ask` (explicit, or downgraded from `auto` because the pending-symbol count exceeds `REMY_SYMBOL_AUTO_SIZE_GUARD`). While symbols are skipped, the file/cluster bootstrap and the propagation pass are skipped too; the call graph itself is complete and zero-cost. Absent line → skipped.

### Phase 3: Hierarchical Bootstrap Confirmation (Conditional)

If `run.py` stdout contains `BOOTSTRAP_PENDING_CONFIRMATION`, the skill asks whether to generate file/cluster summaries via `--bootstrap-only --mode auto`. Triggered only when `REMY_SUMMARY_BOOTSTRAP_MODE=ask` (explicit or downgraded from `auto`). Absent line → skipped.

### Phase 4: Injection Strategy

Based on the `REMY_LOGIC_INDEX_AUTO_INJECT` policy:

| Policy | Behavior |
| :--- | :--- |
| `ALWAYS` (default) | Automatically injects `logic_index.db` into `CLAUDE.md` |
| `ASK` | Prompts user for confirmation before injection |
| `NEVER` | Only generates files, no injection |

## Output Format

`logic_index.db` is structured as:

```markdown
## 🏗️ API Layer
### 📄 `routes/auth.py`
> Imports: models/user.py, services/auth_service.py
- **[f]** `login(request)`: Handles login requests
- **[f]** `register(request)`: Handles registration requests

## 🏗️ Service Layer
### 📄 `services/auth_service.py`
> Imports: models/user.py, utils/token.py
- **[f]** `verify_credentials(email, password)`: Verifies user credentials
- **[f]** `create_session(user)`: Creates session token

## 🏗️ Core
### 📄 `main.py`
> Imports: routes/auth.py, config.py
- **[f]** `main()`: Application entry point
```

## Installing tree-sitter (Optional)

C/C++ and TypeScript/TSX parsing uses regex mode by default (zero dependencies). Install tree-sitter for higher precision and call graph extraction:

```bash
pip install tree-sitter tree-sitter-c tree-sitter-cpp tree-sitter-typescript
```

**C/C++**:

| Feature | Regex Mode | tree-sitter Mode |
| :--- | :--- | :--- |
| Function/struct/enum/macro | Supported | Supported |
| Class methods | Supported | Supported |
| Namespace nesting | Outer only | All levels |
| Template class | Not supported | Supported |
| Call graph extraction | Not supported | Supported |

**TypeScript/TSX**:

| Feature | Regex Mode | tree-sitter Mode |
| :--- | :--- | :--- |
| function/class/interface/enum/type/namespace | Supported | Supported |
| Arrow functions (`export const foo = () => {}`) | Not supported | Supported |
| Abstract class methods | Not supported | Supported |
| Nested namespace members | Not supported | Supported |
| Call graph extraction | Not supported | Supported |

## Configuration

### Remy configuration

Configure user defaults in `~/.claude/remy-config.json` and project overrides in
`<project>/.claude/remy-config.json`. `remy-cc config` writes the user file;
`remy-cc config --path <project>` writes the project file. Process environment
variables with the same `REMY_*` names override both files for that process tree.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `REMY_LLM_API_KEY` | — | API key; user configuration or process environment only |
| `REMY_LLM_MODEL` | `deepseek-v4-flash` | Model name |
| `REMY_LLM_BASE_URL` | `https://api.deepseek.com/v1/chat/completions` | API endpoint |
| `REMY_LLM_MAX_WORKERS` | `8` | Concurrent threads (range: `1..64`) |
| `REMY_LLM_RETRY_LIMIT` | `8` | Retry count (range: `0..32`; retry delay capped at 60 seconds) |
| `REMY_LLM_TIMEOUT` | `300` | Timeout in seconds (range: `30..3600`) |
| `REMY_LLM_MAX_TOKENS` | `32768` | Response token limit (range: `1024..1048576`) |
| `REMY_LLM_TLS_INSECURE` | `false` | Disable TLS certificate verification for the LLM endpoint (insecure); user configuration or process environment only |
| `REMY_LOGIC_INDEX_AUTO_INJECT` | `ALWAYS` | `ALWAYS` / `ASK` / `NEVER` |
| `REMY_LOGIC_INDEX_FILTER_SMALL` | `false` | Skip LLM summarization for small functions without docstrings |
| `REMY_SYMBOL_SUMMARY_MODE` | `auto` | Symbol-layer summary mode (`auto` / `ask` / `never`); `never` keeps a graph-only index |
| `REMY_SYMBOL_AUTO_SIZE_GUARD` | `300` | Pending-symbol count above which `auto` downgrades to `ask` |
| `REMY_LANG` | `en` | Remy interface and injected-view language (`en` / `zh-CN`); summaries are generated in English |
| `REMY_STRUCT_SCAN_TIMEOUT` | `60` | Lifecycle structural scan timeout in seconds |

`PRECISION_READ_THRESHOLD` remains a Claude skill-protocol setting in
`settings.json`; it is not a Python runtime Remy setting.

### TLS Certificate Verification

LLM endpoint certificates are verified against the system trust store by
default. Earlier versions disabled verification; after upgrading, an endpoint
behind a self-signed or enterprise-proxy certificate fails fast on the first
call with `Error: TLS certificate verification failed (...)` instead of
retrying. Set `REMY_LLM_TLS_INSECURE=true` (user configuration or process
environment only) to restore the previous behavior; this disables certificate
and hostname checks and is insecure.

The LLM channel speaks the OpenAI-compatible Chat Completions protocol only.
Anthropic positions its OpenAI compatibility layer as a testing aid rather
than a production interface; for production Claude access, use a relay that
exposes an OpenAI-compatible endpoint.

### Configuration File (`.claude/logic_index_config`)

Two types of directives:

**Exclusion rules** (`!` prefix): Syntax similar to `.gitignore`, supports wildcards.

```text
!tests/
!**/migrations/
!**/CMakeFiles/
!**/*.o
```

**Layer definitions** (`@layer:` prefix): Assign files to architectural layers.

```text
@layer:API Layer=routes,controller,handler,endpoint,api
@layer:Service Layer=service,usecase,use-case,business
@layer:Data Layer=model,entity,schema,database,db,migration,repository,repo
```

## Symbol Types

| Icon | Meaning | Languages |
| :--- | :--- | :--- |
| `[C]` | Class | Python, C++, TypeScript |
| `[f]` | Function | Python, C, C++, TypeScript |
| `[S]` | Struct | C, C++ |
| `[E]` | Enum | C, C++, TypeScript |
| `[T]` | Typedef / TypeAlias | C, C++, TypeScript |
| `[M]` | Macro | C, C++ |
| `[N]` | Namespace | C++, TypeScript |
| `[I]` | Interface | TypeScript |

## Cost Control

- **Docstring/Doxygen priority**: Symbols with documentation incur zero API cost.
- **Short function skip**: Functions under 3 lines without documentation are auto-tagged.
- **Dependency-aware incremental updates**: Only regenerates on actual changes.

## Troubleshooting

### Q: `Fatal API Error 429: Rate limit exceeded`?
Set `REMY_LLM_MAX_WORKERS` to `1` (serial mode), or request a higher quota.

### Q: `Fatal API Error 403: Forbidden`?
Check that `REMY_LLM_API_KEY` is correct and `REMY_LLM_MODEL` is available on the service.

### Q: Will progress be lost if interrupted?
Summaries of completed file batches are written to `.claude/logic_index.db` as each batch finishes; an interruption only loses unfinished batches. Rerunning resumes from the pending state without re-calling completed summaries.

### Q: C/C++/TypeScript call graph not extracted?
Install `tree-sitter` packages. Call graph extraction requires AST precision that regex mode cannot provide. Python call graph works without tree-sitter (uses stdlib `ast`).

### Q: Layer assignments are incorrect for my project?
Edit `.claude/logic_index_config` to customize layer patterns. Delete lines you don't need and add your own. Unmatched files default to "Core".

### Q: Hook enrichment not appearing?
Verify that `~/.claude/settings.json` contains a `hooks.PreToolUse` entry with matcher `Read|Glob|Grep` pointing at the managed `remy-cc hook enrich` command (the binary's hook clients are the only install mode). Run `remy-cc verify` to validate the installation.
