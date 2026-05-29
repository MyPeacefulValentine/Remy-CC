# remy-ci

Analyze CI/CD failure logs to diagnose build, test, and gate failures. Produces evidence packets for `/remy-patch`.

## Usage

```
/remy-ci [run_id | log_file_path | --paste]
```

- **No arguments**: Guided mode — prompts for input source.
- **Numeric argument**: GitHub Actions run ID (requires `gh` CLI).
- **File path**: Read and analyze a local log file.
- **`--paste`**: Manually paste log content.

## Supported Failure Types

| Type | Examples |
| :--- | :--- |
| Compile Error | gcc/clang errors with file:line:col format |
| Link Error | undefined reference, multiple definition |
| Test Failure | pytest, gtest, kunit, TAP format |
| Sanitizer Report | KASAN, UBSAN, KCSAN, ASan, TSan |
| QEMU / Emulation | Kernel panic, boot failure, timeout |
| Style Check | checkpatch, clang-format, linters |
| Static Analysis | sparse, smatch, Coverity, clang-tidy |
| Build Config | Kconfig errors, missing dependencies |

## Input Modes

### Mode A — Paste

No external dependencies. Paste error output directly.

### Mode B — Local File

Read a log file saved from CI output.

### Mode C — GitHub Actions (`gh` CLI)

Requires `gh` CLI installed and authenticated (`gh auth login`). Fetches structured job metadata and failed step logs automatically. If no run ID is provided, detects the latest failed run on the current branch.

## Output

- **Diagnosis report**: `.claude/temp_ci/ci_{timestamp}.md`
- **Evidence packet**: `.claude/temp_task/ci_{timestamp}.json` (compatible with `/remy-patch`)

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CI_LOG_MAX_LINES` | `500` | Max lines retained per failed step |

## Requirements

- `gh` CLI (optional, for Mode C only) — install via https://cli.github.com
