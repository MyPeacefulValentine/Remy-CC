---
name: remy-index
description: Update the semantic logic index (.claude/logic_tree.md) using LLM AST analysis.
allowed-tools: Read, Grep, Glob, Bash, Edit, Write
disable-model-invocation: true
---

# Update Logic Index

Updates the semantic understanding of the codebase by scanning source files (Python, C, C++, TypeScript), extracting symbol signatures, and generating incremental summaries using an OpenAI-compatible LLM API.

Supports multi-language projects. Language is detected automatically by file extension:
- Python: `.py`
- C: `.c`, `.h`
- C++: `.cpp`, `.hpp`, `.cc`, `.cxx`, `.hh`, `.hxx`
- TypeScript: `.ts`, `.tsx`

C/C++ and TypeScript/TSX parsing use regex-based extraction by default (zero external dependencies). Install `tree-sitter` for higher precision (optional):
```bash
pip install tree-sitter tree-sitter-c tree-sitter-cpp tree-sitter-typescript
```

## Process

You MUST execute the following steps strictly in order.

### Step 1: Check Configuration
1.  Check if `.claude/logic_index_config` exists.
2.  **If missing**:
    - Create it from `~/.claude/skills/remy-index/default_logic_config.template`.
    - Use `AskUserQuestion` to prompt: "Configuration generated. Please review `.claude/logic_index_config` to exclude unnecessary directories (to save Tokens). Continue?"
    - Wait for "Yes".
3.  **If exists**:
    - Read the file content.
    - Use `AskUserQuestion` to prompt: "Existing configuration found. The scan will run based on these rules (consuming Tokens). Proceed?"
    - Wait for "Yes".

### Step 2: Execute Scanning
1.  Check if `.claude/logic_tree.md` exists.
    - If **MISSING**: Output a first-run notice in the language configured by `REMY_LANG` (`zh-CN`: "检测到首次运行。即将执行全量代码库扫描，请耐心等待..." / `en`: "First run detected. Full codebase scan starting, please wait...")
2.  Execute the Python indexer:
    ```bash
    python "~/.claude/skills/remy-index/run.py"
    ```
3.  Wait for completion.

### Step 3: Injection Strategy
1.  Determine the injection policy from `settings.json` (env `LOGIC_INDEX_AUTO_INJECT`).
    - Default is `ALWAYS` if not set.

2.  **Branching Logic**:

    - **Case A: Policy == ALWAYS**
        - Execute Injection immediately:
          ```bash
          LOGIC_INDEX_AUTO_INJECT=ALWAYS python "~/.claude/hooks/doc_manager/injector.py"
          ```

    - **Case B: Policy == ASK**
        - Use `AskUserQuestion` to prompt: "Logic Index generated. Inject into CLAUDE.md context?"
        - Options: ["Yes (Inject)", "No (Skip)"]
        - **If Yes**:
          ```bash
          LOGIC_INDEX_AUTO_INJECT=ALWAYS python "~/.claude/hooks/doc_manager/injector.py"
          ```
        - **If No**:
          - Output: "Skipping injection."

    - **Case C: Policy == NEVER**
        - Output: "Skipping injection (Policy: NEVER)."

## Configuration (Environment)

Requires the following environment variables (injected via `settings.json` > `env`):

- `OPENAI_API_KEY`: API Key for OpenAI-compatible service (e.g., Aliyun Bailian).
- `OPENAI_MODEL`: Model name (default: `glm-5`).
- `OPENAI_MAX_WORKERS`: Concurrency limit (default: 5).
- `OPENAI_BASE_URL`: API endpoint (default: `https://coding.dashscope.aliyuncs.com/v1/chat/completions`).

## Feature Flags

- `LOGIC_INDEX_AUTO_INJECT`: Controls automated injection of `logic_tree.md` into `CLAUDE.md`.
    - `ALWAYS`: Automatically update CLAUDE.md after indexing.
    - `ASK`: Prompt user for confirmation before injection.
    - `NEVER`: Only generate files, do not inject.
- `LOGIC_INDEX_FILTER_SMALL`: Skip LLM summarization for small (< 3 lines) functions without docstrings. (Default: `false`)

## Output

Generates:
1. `.claude/logic_index.json`: Incremental cache of AST hashes and summaries.
2. `.claude/logic_tree.md`: Readable Markdown tree for context injection.
    - Includes `Last Updated` timestamp.
    - Includes `Git Commit` short hash (if git is available) for version tracking.
