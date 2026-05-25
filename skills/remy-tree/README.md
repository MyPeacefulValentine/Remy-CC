# remy-tree (Project Tree Snapshot)

remy-tree generates a text snapshot of the project directory structure and saves it to `.claude/project_tree.md`. This snapshot is injected into `CLAUDE.md` as a structural navigation reference for the AI. The tree is also auto-updated on session lifecycle events.

## When to Use

- After batch file operations (creating, moving, or deleting multiple files)
- After refactoring that changes module structure or directory layout
- After destructive actions (`rm`, `mv`) that alter directory structure
- When the AI begins referencing non-existent file paths (stale context)

## Workflow

### Manual Invocation

1. Run `/remy-tree`. The skill executes `generate_smart_tree.py` to produce `.claude/project_tree.md`.
2. The document injector (`hooks/doc_manager/injector.py`) updates the `CLAUDE.md` reference.

### Automatic Updates

The tree is also updated automatically via `hooks/tree_system/lifecycle_hook.py`:

| Event | Trigger |
| :--- | :--- |
| `SessionStart` | Session begins |
| `PreCompact` | Before context compaction |
| `SessionEnd` | Session ends |

## Configuration

The skill reads `.claude/tree_config` for rules. If the file does not exist, a default template is created on first run.

### Syntax

- **Exclusion rules** (`!` prefix):
    - `!node_modules` — exclude directories/files named node_modules
    - `!*.log` — exclude files ending in .log
- **Inclusion rules** (`[path] [arguments]`):
    - `-depth N` — traversal depth (0 = current level only, -1 = unlimited)
    - `-if_file true/false` — whether to list individual files

### Example

```text
# Exclusions
!__pycache__
!.git
!dist

# Root: depth 2, show files
. -depth 2 -if_file true

# Assets: depth 1, directories only
src/assets -depth 1 -if_file false

# Core code: unlimited depth, show files
src/core -depth -1 -if_file true
```

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
| `../../hooks/tree_system/generate_smart_tree.py` | Tree generation script |
| `../../hooks/tree_system/lifecycle_hook.py` | Session lifecycle handler |
| `../../hooks/tree_system/default_tree_config.template` | Default configuration template |
| `../../hooks/doc_manager/injector.py` | CLAUDE.md reference injection |
