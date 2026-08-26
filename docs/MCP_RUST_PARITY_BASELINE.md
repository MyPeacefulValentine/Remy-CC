# MCP Rust Parity Baseline (H.4)

Version: v0.1 (2026-08-24). Source: window research notes 2026-08-22 §4, formalized by the
R4.1 pre-migration preparation batch (packet `task_20260824_213739`).

This document is the cross-implementation verification contract for R4.1: the Rust MCP
server must reproduce the Python MCP server's output over a frozen corpus before the
Python server retires. The oracle is the **post-stabilization** Python output — after the
preparation batch added the Rust language filter, deterministic secondary ordering, and
the bounded `key symbols` section of `query_file_summary`.

## 1. Snapshot identity

Every differential run records, and the comparator refuses to compare across a mismatch of:

- DB snapshot SHA-256 (the frozen `logic_index.db` file);
- git commit of the tree that produced the snapshot;
- logic index schema version (`12.0.0` anchor);
- comparator/tooling version used for the diff;
- config snapshot of every `REMY_MCP_*`, `REMY_FLOW_*`, and `REMY_NAVIGATE_*` key read by
  the query layer.

## 2. Frozen DB snapshot — structural requirements (R1–R8)

The snapshot must contain the following structures; otherwise the corresponding
non-deterministic or ambiguous paths are untested:

- **R1** a multi-file same-short-name symbol pair (query_symbol ambiguity, search tie-break);
- **R2** a node without a summary, a `stale`-status node, and an `oversized_warn` node
  (full enumeration of status wording);
- **R3** a cluster pair tied on `file_count` (secondary sort key corpus — the key ships in
  the preparation batch);
- **R4** synthesized edges (interface, observer, rust_trait — one each) for the
  `static_only` arm;
- **R5** at least one `rust_trait_impl` patterns row;
- **R6** a BM25 rank tie/near-tie retrieval pair (float-drift sensitive);
- **R7** a pre-warmed `judge_cache` row (navigate cache-hit path);
- **R8** stored `source_commit` equal to the snapshot's git HEAD (makes the
  `random.sample` freshness branch unreachable).

## 3. Per-tool query matrix (each tool ≥3 groups: normal / empty / ambiguous-variant)

| Tool | Group 1 normal | Group 2 empty | Group 3+ ambiguity/variants | Comparison layer |
| :--- | :--- | :--- | :--- | :--- |
| query_symbol | unique qualified name | missing name | multi-file short name; +file filter | byte-for-byte |
| query_symbol_summary | summarized symbol | missing name | unsummarized symbol; stale symbol (R2) | byte-for-byte |
| query_file_summary | summarized file | missing file | unsummarized file; 0-symbol file; key-symbols truncation (> `REMY_MCP_RESULT_LIMIT`) | byte-for-byte |
| query_callers | depth=2 default | symbol with no callers | static_only=True; include_ambiguous=True; depth=1 | byte-for-byte |
| query_callees | depth=2 default | leaf function | static_only=True; include_ambiguous=True | byte-for-byte |
| query_impact | single file | missing file | multiple files; cross-layer files | byte-for-byte |
| query_patterns | no-arg full listing | missing signal | pattern_type=rust_trait_impl; file filter | byte-for-byte |
| query_search | match=all normal | no-hit text | match=any; phrase; typo → fuzzy; language (incl. `rust`) / path_hint filters; R6 tie pair | semantic layer (node_ref sequence + rank tolerance or order-only) |
| query_flow | two connected symbols | two disconnected symbols | three symbols; qualified syntax (file:name / Class.method); static_only | byte-for-byte |
| query_cluster_summary | name="" all clusters (incl. R3 tie pair) | missing cluster | single cluster | byte-for-byte (secondary key ships with the preparation batch) |
| query_cluster_files | normal cluster | missing cluster | with_summary=True | byte-for-byte |
| query_navigate | R7 cache-hit intent | — (no empty group at the LLM layer) | top_k=1 | semantic layer, judge_cache **hit path only**; miss path excluded from the baseline |

## 4. Comparison layers and exclusions

- **Byte-for-byte** applies after stripping the freshness warning prefix (the `[Warning:
  index may be stale …]` line is startup-state dependent and not part of the contract).
- **Semantic layer** (search/navigate): compare the ordered node_ref sequence only; BM25
  rank values are not asserted (R4.1 decision, 2026-08-26 — the Python stdlib SQLite and
  the rusqlite bundled SQLite differ in version, so float rank equality is not
  guaranteed and a numeric tolerance would need recalibration on every SQLite bump).
- **Excluded from the baseline**: navigate LLM miss-path behavior; freshness sampling
  randomness (unreachable given R8; the **N2** seed seam shipped with the R4.1 first
  commit — `REMY_FRESHNESS_SAMPLE_SEED` switches the fallback branch to a sorted,
  seed-rotated deterministic subset reproducible across implementations; the env key is
  a test seam, not a registered config field); non-ASCII identifier behavior in
  search/navigate (casefold / Unicode category / fuzzy-ratio equivalence is guaranteed
  for ASCII identifiers only — the entire indexed corpus today; declared boundary for
  future non-ASCII corpora).

### 4.1 Allowed tool-schema differences (R4.1 decision, 2026-08-26)

The rmcp/schemars output may differ from the FastMCP oracle in exactly these
decoration-layer items; anything beyond this list is a defect:

| Item | FastMCP | rmcp/schemars |
| :--- | :--- | :--- |
| per-property `title` | present ("Max Depth" etc.) | absent |
| top-level `title` | `query_xxxArguments` | absent |
| top-level `$schema` | absent | `https://json-schema.org/draft/2020-12/schema` |
| integer `format` | absent | `int64` |

Closure action: one real Claude Code session invoking all 12 tools against the Rust
server, verifying parameter parsing and callability.

### 4.2 Single-implementation tools (decision, 2026-08-26)

Tools implemented only in Rust with no Python oracle arm are **excluded from the
differential matrix**: a freshly written Python reference would share no independent
correctness anchor with the Rust implementation (mutual comparison is circular), and
adding code to the retiring Python server contradicts the retirement direction. Their
acceptance surface is a dedicated test suite instead, which must include a DB
immutability assertion (snapshot hash unchanged across calls).

Current single-implementation tools: `query_dependencies` (suite:
`tests/test_mcp_dependencies.py`). The tool-listing assertion in
`tests/test_mcp_rust_parity.py` tracks them via `RUST_ONLY_TOOLS`.

Consequence for the eval harness: `eval/arms.py` enumerates the tool surface from the
Python oracle's `list_tools()`, so single-implementation tools never enter the eval
arms — the eval tool surface is a strict subset of the production surface.

## 5. Status

- N5 (cluster list secondary sort key): **shipped in the preparation batch** —
  `query_cluster_summary(name="")` qualifies for byte-for-byte comparison.
- N2 (freshness sampling seed): **shipped in the R4.1 first commit** — deterministic
  subset mode behind `REMY_FRESHNESS_SAMPLE_SEED` (sorted by path, rotated by seed).
- `query_search` accepts `language="rust"` and `query_file_summary` emits the bounded
  `key symbols` section as of the preparation batch; the Rust implementation reproduces
  these as part of the oracle.
