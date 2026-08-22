# Saturation Protocol — Shared Rules (Owner)

Single authoritative definition of the context-saturation rules shared by every consumer below. Consumer protocols carry a self-sufficient compact summary of these rules plus a reference to this file; when a summary and this file diverge, this file wins.

**Consumers**: `remy-plan` (Phase 1), `remy-patch` (Phase 1), `remy-milestone` (Phase 1) — skill protocols; `CLAUDE.md` "Recursive Context Integrity" block — per-session injection layer, kept in full as an anti-decay baseline.

## Principle Layer (MUST)

1. **Self-Correction**: Ask "Do I have the *source definition* of every dependency involved?"
2. **Recursive Read**: NEVER infer a function/variable's definition solely from its usage. If you only see usages (e.g., `db.connect()`), you MUST read the definition (e.g., `class DBConnection`).
3. **Inheritance Recursion**: If a definition inherits from a parent/interface, you MUST retrieve the parent's definition to verify the full type signature.
4. **No Hallucinations**: You are FORBIDDEN from assuming implementation details without evidence. Do not proceed until context is "saturated" (no ambiguous types remain).

## Precision-Read Rule (Operational Layer)

For every file to be read during saturation (e.g., every file at Upstream Depth 1 and Downstream Depth 1 of an impact report, or all grep-discovered files in the manual path):

- If line ranges are available (e.g., `[L120-L155]`) **and** the file exceeds `PRECISION_READ_THRESHOLD` lines (default: 500), use `Read(file_path, offset=start_line, limit=end_line - start_line + 1)` for each listed function instead of reading the full file. Group adjacent functions into a single Read when their ranges overlap or are within 10 lines of each other.
- Otherwise, `Read` the entire file.
