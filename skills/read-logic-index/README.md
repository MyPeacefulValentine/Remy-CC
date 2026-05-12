# Read Logic Index (Index Viewer)

Read Logic Index displays the current semantic code index from `.claude/logic_tree.md`. If the index does not exist, it offers to generate one.

## When to Use

- To view the project's function/class structure and architecture layers
- To check the current state of the logic index before or after updates

## Workflow

1. Checks if `.claude/logic_tree.md` exists.
2. **If found**: Reads and displays the content.
3. **If missing**: Asks the user whether to run `/update-logic-index` to generate it.

## Related Files

| File | Purpose |
| :--- | :--- |
| `SKILL.md` | Protocol definition (loaded by Claude Code) |
