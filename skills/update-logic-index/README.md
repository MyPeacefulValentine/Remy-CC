# Logic Indexer v3 (Semantic Code Index)

Logic Indexer is a semantic indexing tool based on multi-language source code parsing and an OpenAI-compatible API. It parses Python, C, C++, and TypeScript/TSX code to generate architecture-layered semantic summaries with call graph data, enabling Claude Code to understand project structure and function relationships without reading full source files.

## Architecture Overview

The system operates on three layers:

```
┌──────────────────────────────────────────────────────────┐
│  logic_tree.md (injected into CLAUDE.md)                 │
│  ├── Architecture layer grouping (files grouped by layer)│
│  ├── File-level summaries + imports annotations          │
│  └── Symbol-level signatures + summaries                 │
│  Purpose: Baseline cognition (structure, roles, deps)    │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  logic_index.json (disk cache, not injected)             │
│  ├── Symbol hashes + summary cache                       │
│  ├── File-level imports list                             │
│  ├── File layer assignment                               │
│  └── Function-level CALLS edges (with callee resolution) │
│  Purpose: Hook query source + incremental build cache    │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  PreToolUse Hook (passive enrichment)                    │
│  Trigger: Claude Code executes Read/Grep/Glob            │
│  Behavior: Queries logic_index.json for target file's    │
│           callers/callees/layer, appends to hook output  │
│  Purpose: On-demand relationship info without MCP        │
└──────────────────────────────────────────────────────────┘
```

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

`hooks/logic_enrichment_hook.py` is a PreToolUse hook triggered on Read/Glob/Grep operations. It queries `logic_index.json` for the target file and outputs:

```
[Logic Context] services/auth.py (Service Layer)
  Calls into: models/user.py::User.verify_password, utils/token.py::generate_jwt
  Called by: routes/login.py::handle_login, routes/register.py::handle_register
```

This provides relationship context without requiring Claude Code to proactively call MCP tools.

### Multi-Language Parsing

- **AST Parsing (Python)**: Identifies Class, Function, and Method structures.
- **Regex + tree-sitter Dual Path (C/C++/TypeScript)**: Zero-dependency regex mode by default; automatically switches to high-precision mode when tree-sitter is installed.

### Cross-File Context

- Parses Python `import`, C/C++ `#include "..."`, and TypeScript relative `import` dependencies.
- Injects upstream module summaries into LLM prompts for context-aware summarization.
- Displays import list per file in `logic_tree.md` output.

### Incremental Updates

- **File-Level Hashing**: MD5-based source content hashing.
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

## Workflow (3 Steps)

### Step 1: Check Configuration

The skill checks for `.claude/logic_index_config`. If missing, it creates one from the default template (including layer definitions and exclusion rules) and prompts the user to review before proceeding.

### Step 2: Execute Scanning

Runs the Python indexer:

```bash
python "~/.claude/skills/update-logic-index/run.py"
```

On first run (no existing `.claude/logic_tree.md`), a full codebase scan is performed. The indexer:
1. Walks the project tree, parses symbols and call graphs per file
2. Resolves callee names to qualified references using import maps
3. Generates LLM summaries for symbols without documentation
4. Saves results to `.claude/logic_index.json` (cache) and `.claude/logic_tree.md` (output)

### Step 3: Injection Strategy

Based on the `LOGIC_INDEX_AUTO_INJECT` policy:

| Policy | Behavior |
| :--- | :--- |
| `ALWAYS` (default) | Automatically injects `logic_tree.md` into `CLAUDE.md` |
| `ASK` | Prompts user for confirmation before injection |
| `NEVER` | Only generates files, no injection |

## Output Format

`logic_tree.md` is structured as:

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

### Environment Variables (`settings.json`)

Configure in `settings.local.json` (project-level) or `~/.claude/settings.json` (global):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `OPENAI_API_KEY` | — | API key |
| `OPENAI_MODEL` | `deepseek-v4-flash` | Model name |
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1/chat/completions` | API endpoint |
| `OPENAI_MAX_WORKERS` | `3` | Concurrent threads |
| `OPENAI_RETRY_LIMIT` | `3` | Retry count |
| `OPENAI_TIMEOUT` | `300` | Timeout in seconds |
| `OPENAI_MAX_TOKENS` | `8192` | Response token limit |
| `LOGIC_INDEX_AUTO_INJECT` | `ALWAYS` | `ALWAYS` / `ASK` / `NEVER` |
| `LOGIC_INDEX_FILTER_SMALL` | `false` | Skip LLM summarization for small functions without docstrings |
| `REMY_LANG` | `en` | Summary output language (`en` / `zh-CN`) |
| `IMPACT_DEPTH_UP` | `2` | Default upstream (callers) BFS depth for `impact.py` |
| `IMPACT_DEPTH_DOWN` | `2` | Default downstream (callees) BFS depth for `impact.py` |

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
Set `OPENAI_MAX_WORKERS` to `1` (serial mode), or request a higher quota.

### Q: `Fatal API Error 403: Forbidden`?
Check that `OPENAI_API_KEY` is correct and `OPENAI_MODEL` is available on the service.

### Q: Will progress be lost if interrupted?
No. The `try...finally` protection mechanism ensures generated summaries are saved to `.claude/logic_index.json`.

### Q: C/C++/TypeScript call graph not extracted?
Install `tree-sitter` packages. Call graph extraction requires AST precision that regex mode cannot provide. Python call graph works without tree-sitter (uses stdlib `ast`).

### Q: Layer assignments are incorrect for my project?
Edit `.claude/logic_index_config` to customize layer patterns. Delete lines you don't need and add your own. Unmatched files default to "Core".

### Q: Hook enrichment not appearing?
Verify `logic_enrichment_hook.py` is registered in `~/.claude/settings.json` under `hooks.PreToolUse` with matcher `Read|Glob|Grep`. Run `python install.py --verify` to check.
