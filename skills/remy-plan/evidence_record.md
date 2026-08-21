# Evidence Record — Shared Rules (Owner)

Single authoritative definition of the evidence rules shared by every skill that produces or consumes an AgentTaskPacketLite evidence packet. Consumer skills carry a self-sufficient one-line summary of these rules plus a reference to this file; when the summary and this file diverge, this file wins.

**Consumers**: `remy-plan` (Phase 6), `remy-debug` (Phase 4), `remy-ci` (Phase 5), `remy-testgen` (Phase 7.3) — producers; `remy-patch` (Phase 0, H1–H3) — consumer. The PreToolUse guard (`hooks/pre_tool_guard.py::validate_packet`) mechanically enforces rules 4 and the `suspected`/`stale` block below; the remaining rules are protocol obligations on the producing skill.

## Core Rules (MUST)

1. **Actually-read only**: `evidence[]` contains one item per file ACTUALLY READ during the producing session. Unread files MUST NOT appear.
2. **Verbatim excerpt**: `excerpt` is MANDATORY verbatim text from the cited `range`. Summaries and paraphrases are prohibited.
3. **Status semantics**: `status: "confirmed"` only for files read in the producing session; `status: "suspected"` for inferred but unread files.
4. **Confirmed refs**: every `proposed_changes[].evidence_refs` MUST reference at least one evidence ID with `status: "confirmed"`. The guard hook rejects any write while a referenced entry is `suspected` or `stale`.
5. **Non-git fallback**: if the project is NOT a git repository, `source_revision` uses `"type": "filesystem"` and omits `"commit"`.

## Status State Machine

```
suspected --(re-read path+range, content matches)--> confirmed
confirmed --(underlying file changed since excerpt)--> stale plan (remy-patch H2 hard interrupt)
```

- **Promotion** (`remy-patch` Phase 0): for any `suspected` entry, re-read the referenced `path` and `range`, confirm the content, then edit the packet to promote `status` to `"confirmed"`. Writes under `.claude/temp_task/` are exempt from the guard, so the promotion edit is always permitted.
- **Staleness** (`remy-patch` H2): if the target's signature or structure has changed since the audit's `excerpt`, the plan is stale — hard interrupt, do not edit.

## Producer-Specific Extensions (defined in each SKILL.md, not here)

- `remy-plan`: `action` required per change; `caller_refs` required for executable-code Create entries.
- `remy-debug` / `remy-ci`: inconclusive diagnosis downgrades `task.mode` to `"investigate"`.
- `remy-testgen`: generated test files count as read by construction, hence `status: "confirmed"`, `confidence: 1.0`.
