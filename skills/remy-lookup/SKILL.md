---
name: remy-lookup
description: Display the current logic index tree (.claude/logic_tree_view.md) without regenerating.
allowed-tools: Read, Grep, Glob
disable-model-invocation: true
---

# Protocol: Read Logic Index

You MUST execute the following steps strictly in order. Do not skip steps.

## Step 1: Verification
Check if the file `.claude/logic_tree_view.md` exists in the current working directory.
- Use `ls .claude/logic_tree_view.md` or `Glob` to verify existence.

## Step 2: Branching Logic
**Case A: File Exists**
1. Execute `Read` tool on `.claude/logic_tree_view.md`.
2. Output the content directly to the user.
3. Terminate.

**Case B: File Missing**
1. Use `AskUserQuestion` to ask the user if they want to generate it now.
   - Question: "Logic index not found. Generate it now?"
   - Options: ["Yes (Run /remy-index)", "No (Cancel)"]
2. If "Yes":
   - Execute `/remy-index`.
   - Upon completion, recurse to Step 1 (Verification).
3. If "No":
   - Terminate.
