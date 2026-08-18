---
name: remy-index
description: Scan source files (Python, C/C++, TypeScript, Rust) and generate semantic logic index via LLM. Index is injected into CLAUDE.md and used by hooks/skills for dependency analysis. Run after repo init or major changes.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
disable-model-invocation: true
---

# Update Logic Index

Updates the semantic understanding of the codebase by scanning source files (Python, C, C++, TypeScript, Rust), extracting symbol signatures, and generating incremental summaries using an OpenAI-compatible LLM API.

Supports multi-language projects. Language is detected automatically by file extension:
- Python: `.py`
- C: `.c`, `.h`
- C++: `.cpp`, `.hpp`, `.cc`, `.cxx`, `.hh`, `.hxx`
- TypeScript: `.ts`, `.tsx`
- Rust: `.rs`

C/C++ and TypeScript/TSX parsing use regex-based extraction by default (zero external dependencies). Rust parsing requires the tree-sitter grammar (no regex fallback; without it `.rs` files fail scanning and keep previously indexed rows). Rust `impl Trait for Type` blocks are recorded as per-file `rust_trait_impl` facts; the global postprocess derives struct/enum `bases` from them (cross-file impls included, ambiguous short-name targets merge nothing) and synthesizes `trait-impl` edges at the impl site. Install `tree-sitter` for higher C/C++/TS precision and for Rust support:
```bash
pip install tree-sitter tree-sitter-c tree-sitter-cpp tree-sitter-typescript tree-sitter-rust
```

## Process

You MUST execute the following steps strictly in order.

### Phase 1: Check Configuration
1.  Check if `.claude/logic_index_config` exists.
2.  **If missing**:
    - Create it from `~/.claude/skills/remy-index/default_logic_config.template`.
    - Use `AskUserQuestion` to prompt: "Configuration generated. Please review `.claude/logic_index_config` to exclude unnecessary directories (to save Tokens). Continue?"
    - Wait for "Yes".
3.  **If exists**:
    - Read the file content.
    - Use `AskUserQuestion` to prompt: "Existing configuration found. The scan will run based on these rules (consuming Tokens). Proceed?"
    - Wait for "Yes".

### Phase 2: Execute Scanning
1.  Check if `.claude/logic_index.db` exists.
    - If **MISSING**: Output a first-run notice in the language specified by the loaded `language.md` directive.
2.  Execute the Python indexer:
    ```bash
    python "~/.claude/skills/remy-index/run.py"
    ```
3.  Wait for completion and inspect the exit code:
    - `0` (`success`): continue normally.
    - `2` (`partial`): report the incomplete stages, then continue to Phase 3 so any bootstrap confirmation marker is still handled.
    - `1` (`failed`): stop. Do not run injection; dirty paths remain queued for retry.

### Phase 3: Hierarchical Bootstrap Confirmation (Trigger: run.py stdout contains BOOTSTRAP_PENDING_CONFIRMATION)

After Phase 2, inspect the stdout produced by `run.py` and decide whether to ask the user about generating file/cluster summaries:

1.  **Search the captured stdout for the line `BOOTSTRAP_PENDING_CONFIRMATION`.** This line is emitted only when `REMY_SUMMARY_BOOTSTRAP_MODE=ask` (explicit or downgraded from `auto` due to a missing API key or file count exceeding `REMY_BOOTSTRAP_AUTO_SIZE_GUARD`).
2.  **If the line is absent**: Skip Phase 3 entirely. `auto` mode already generated file/cluster summaries; `never` mode intentionally skipped.
3.  **If the line is present**: Parse the `pending_files=X pending_clusters=Y` fields, then use `AskUserQuestion` in the language specified by the loaded `language.md` directive:
    - Question (zh-CN): "检测到 {X} 个文件 + {Y} 个集群尚未生成层级摘要。立即生成？（消耗 LLM tokens）"
    - Question (en): "{X} file(s) and {Y} cluster(s) lack hierarchical summaries. Generate now? (consumes LLM tokens)"
    - Options:
        - "Yes — generate now (推荐)" — proceed to step 4.
        - "Skip this session" — proceed to step 5.
        - "Disable hierarchical summaries permanently" — proceed to step 6.
4.  **If user chose "Yes"**: Execute the bootstrap-only entry, overriding the mode to `auto`:
    ```bash
    python "~/.claude/skills/remy-index/run.py" --bootstrap-only --mode auto
    ```
    Wait for completion. The output again contains a `BOOTSTRAP_RESULT` line confirming file_done / cluster_done counts.
5.  **If user chose "Skip"**: Output a single notice: "Skipped. Run `/remy-index` again or `remy-cc summary-rebuild` later to retry."
6.  **If user chose "Disable permanently"**: Output the command for the user to apply manually: "Set `REMY_SUMMARY_BOOTSTRAP_MODE=never` through `remy-cc config --path <project>`."

### Phase 4: Injection Strategy
1.  Determine the injection policy from the effective Remy configuration key `REMY_LOGIC_INDEX_AUTO_INJECT`.
    - Default is `ALWAYS` if not set.

2.  **Branching Logic**:

    - **Case A: Policy == ALWAYS**
        - Execute Injection immediately:
          ```bash
          REMY_LOGIC_INDEX_AUTO_INJECT=ALWAYS python "~/.claude/hooks/doc_manager/injector.py"
          ```

    - **Case B: Policy == ASK**
        - Use `AskUserQuestion` to prompt: "Logic Index generated. Inject into CLAUDE.md context?"
        - Options: ["Yes (Inject)", "No (Skip)"]
        - **If Yes**:
          ```bash
          REMY_LOGIC_INDEX_AUTO_INJECT=ALWAYS python "~/.claude/hooks/doc_manager/injector.py"
          ```
        - **If No**:
          - Output: "Skipping injection."

    - **Case C: Policy == NEVER**
        - Output: "Skipping injection (Policy: NEVER)."

## Configuration

`run.py` reads the independent Remy configuration files through the shared
registry. User values live in `~/.claude/remy-config.json`; project overrides
live in `<project>/.claude/remy-config.json`. Same-named process environment
variables override files for the current process tree.

- `REMY_LLM_API_KEY`: API key for an OpenAI-compatible service.
- `REMY_LLM_MODEL`: model name (default: `deepseek-v4-flash`).
- `REMY_LLM_MAX_WORKERS`: concurrency limit (default: `8`, range: `1..64`).
- `REMY_LLM_BASE_URL`: endpoint (default: `https://api.deepseek.com/v1/chat/completions`).
- `REMY_LLM_MAX_TOKENS`: response token limit (default: `32768`, range: `1024..1048576`).

## Feature Flags

- `REMY_LOGIC_INDEX_AUTO_INJECT`: Controls automated injection of `logic_tree_view.md` into `CLAUDE.md`.
    - `ALWAYS`: Automatically update CLAUDE.md after indexing.
    - `ASK`: Prompt user for confirmation before injection.
    - `NEVER`: Only generate files, do not inject.
- `REMY_LOGIC_INDEX_FILTER_SMALL`: Skip LLM summarization for small (< 3 lines) functions without docstrings. (Default: `false`)

## Output

Generates:
1. `.claude/logic_index.db`: SQLite database storing symbols, call graph, patterns, and clusters.
2. `.claude/logic_tree_view.md`: Filtered Markdown view for context injection (generated by injector from DB).
    - Includes `Last Updated` timestamp.
    - Includes `Git Commit` short hash (if git is available) for version tracking.
