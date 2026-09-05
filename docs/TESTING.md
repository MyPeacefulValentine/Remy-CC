# Testing

## Environment

Remy-CC supports Python 3.10 or newer. Install the pinned development tools with:

```bash
python -m pip install -r requirements-dev.txt
```

This file includes pytest, Pyright, and the MCP SDK required by `tests/test_freshness.py`.

Install the optional high-precision parser packages with:

```bash
python -m pip install -r requirements-tree-sitter.txt
```

## Verification baseline

The repository baseline before P0.1a/P0.1b implementation contained 454 collected tests. The P0.6 function-pointer pattern correction verification collected 512 passing tests on 2026-08-01.

```bash
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m pytest tests/test_struct_scan.py tests/test_enrichment_hook.py tests/test_index_state.py -q -p no:cacheprovider
python -m pytest tests/test_migration_ladder.py -q -p no:cacheprovider
python -m pytest tests/test_synthesizers.py tests/test_c_fnptr_dispatch.py -q -p no:cacheprovider
python -m pytest tests/test_struct_scan.py tests/test_fts_three_layer.py -q -p no:cacheprovider
python -m pytest tests/test_tee_canary.py -q -p no:cacheprovider
PYTHONPATH=. python -m eval.cli retrieval-baseline --help
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_1.json --save
python tests/tee_project_canary.py tests/fixtures/tee_canary --fixture --backend regex --scope product
# With requirements-tree-sitter.txt installed:
python tests/tee_project_canary.py tests/fixtures/tee_canary --fixture --backend tree-sitter --scope product
```

CI runs the full suite on Python 3.10 without tree-sitter and Python 3.12 with the pinned tree-sitter packages. Both jobs run the fixed public TEE fixture canary with their available parser backend. A Windows Python 3.12 job executes the process-lock and dirty-queue tests. Both arms of the Rust job install the pinned tree-sitter packages and run `tests/test_scanner_core_diff.py`, the cross-implementation diff suite (per-language fixture corpora, a mixed-language project, and the Python failure mapping; skipped without a cargo binary or tree-sitter).

## P1.1 deterministic retrieval baseline

`eval/tasks/retrieval_baseline/p1_1.json` declares a synthetic schema 10.0.0
fixture and reviewed candidate-level truth. The baseline records each FTS, LIKE,
and fuzzy channel, the public fallback output, Recall@1/5/10, MRR, no-result
metrics, database/WAL sizes, and all latency samples. Each measurement uses three
warmups and thirty recorded iterations. Timing values are observations, not pass
thresholds.

Timestamped raw records under `eval/results/` remain ignored by Git. Updating
`eval/baselines/p1_1.json` requires the explicit `--update-snapshot` option after
review. The command records Git commit, schema, Python version, platform, and the
synthetic parser configuration. When `--navigate-db` is supplied, it also records
cluster/file counts and prompt characters without calling an LLM or writing
`judge_cache`.

## P1.2 query semantics and filters

Run the format 1.1.0 task set and the unchanged P1.1 compatibility comparison:

```bash
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_2.json --update-snapshot eval/baselines/p1_2.json
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_1.json --compare-baseline eval/baselines/p1_1.json --comparison-output eval/baselines/p1_2_compat.json
```

The P1.2 record covers all/any/phrase matching, language/type/path SQL filters,
input and channel errors, insertion-order-independent LIKE/fuzzy results, and
the final fuzzy limit for same-name symbols. Both runs retain three warmups and
thirty measured iterations. The P1.2 fixture remains on schema 10.0.0.

## P1.3 candidate union

`query_search` builds exact, prefix, and BM25 candidates independently, merges
them by `node_ref` while keeping every matching source with its per-source
rank, orders the union by the deterministic priority (exact, then prefix, then
BM25, then per-source rank, name, file, and line), and truncates only after
the merge. Fuzzy runs when all three deterministic channels are empty. A
SQLite error in any channel still returns an `Error:` result without partial
degradation. Each result keeps the previous location line unchanged and adds
indented `sources` / `priority` and `sig` / `summary` lines. Exact matching
uses a registered Python `casefold` SQL function because SQLite `NOCASE` and
`lower()` fold ASCII only. The schema stays at 11.0.0; no migration is
required.

Run the union task set and the compatibility comparisons:

```bash
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_3.json --update-snapshot eval/baselines/p1_3.json
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_1.json --compare-baseline eval/baselines/p1_1.json --comparison-output eval/baselines/p1_3_compat.json
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_2.json --compare-baseline eval/baselines/p1_2.json
```

`p1_3.json` reuses the P1.1 fixture and its sixteen query strings with
union-semantics expectations derived by hand before implementation. The
`summary_name_conflict` task asserts that the name candidate
`encrypt_session_tokens` enters the public result at rank one through the
prefix channel while the summary candidate `persist_blob` remains in the
union. Recorded runs keep the four channel candidate lists, the merged result
with sources and priority, Recall@1/5/10, MRR, latency samples, and database
sizes. The `expected_channel` values in `p1_1.json` and `p1_2.json` record the
previous channel semantics; mismatches against them are the intended channel
reorganization, not defects.

## P1.4 intent navigation candidate reduction

`query_navigate` no longer writes every cluster and file summary into the LLM
prompt. The intent is tokenized and reuses the P1.3 deterministic channels
with `any` semantics for symbol candidates (single-word intents additionally
try the fuzzy channel when all deterministic channels are empty), while
file/cluster candidates come from a weighted BM25 query over the projection
rows. Each layer is capped by `REMY_NAVIGATE_CANDIDATE_CLUSTERS/FILES/SYMBOLS`
(defaults 5/10/10); summaries are read only for selected candidates. When the
lexical candidate set is empty the query degrades to a cluster-only prompt
(`source=llm-cluster-only`); without an LLM the merged candidates are emitted
in deterministic order (`source=heuristic`), or `No matches` when the
candidate set is empty. The cache key is a hash of the normalized intent,
`top_k`, the candidate `(node_ref, content_hash)` sequence, and the prompt
template version, stored in the existing `judge_cache`; summary writes
unrelated to the candidates no longer invalidate the cache, and `top_k` is
part of the key. The schema stays at 11.0.0. In the same change index
summaries become English-only: `run.py` fixes the summary language to
English, cluster tags use the en set, and the `SUMMARY_ZH_LENGTH_FACTOR`
registry field is removed (the key in existing config files no longer
activates and round-trips as an unknown field); existing Chinese summaries
are replaced only on regeneration or `remy-cc summary-rebuild`.

Run the P1.4 task set (tasks reuse p1_3 verbatim to verify the
`query_search` contract does not regress; the navigation block carries
English/Chinese/mixed intents):

```bash
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_4.json --navigate-db .claude/logic_index.db --update-snapshot eval/baselines/p1_4.json
```

Navigation records use a same-database dual measurement: the corpus view
(cluster/file counts, files with summaries, `corpus_chars` — the character
size of the full-corpus prompt equivalent) and the candidate view (per-layer
candidate counts, `prompt_chars`, `fallback_reason`, and the content-identity
cache key). Acceptance asserts `prompt_chars < corpus_chars` and a candidate
total within the quota sum; Chinese intents record
`fallback_reason=lexical_empty` (unicode61 keeps contiguous CJK as a single
token, so lexical channels return empty sets against both Chinese and English
corpora, confirmed by audit probes). The 1346-character measurement in the
p1_1 baseline reflects a corpus scope that has since drifted (zero files had
summaries then) and is not a comparison reference.

## A1.1 run.py responsibility split

`llm_client.py` (class `LlmClient`: HTTP transport, capped exponential retry,
circuit breaker on 401/403/429, truncation detection, error classification)
and `propagation.py` (force-recompute checks, counter resets, candidate
collection, child-change payloads, parent rewrite, propagation pass) are
extracted from `run.py`. `run.py` keeps `LogicIndexer` as the orchestration
and CLI entry: arguments, output statuses, exit codes `0 / 2 / 1`, the
`success / partial / failed` aggregation rule, and dirty confirmation are
unchanged. The default LLM channels in `index_mcp_navigate.py` and `cli.py`
construct `LlmClient` directly (one instance per call; the breaker does not
persist across calls, matching prior behavior).

```
python -m pytest Remy-CC/tests/test_llm_client.py Remy-CC/tests/test_propagation.py -v
```

Equivalence was verified by a one-shot probe, not a permanent golden test:
the pre-split tree (`git archive HEAD`) and the post-split tree ran
`LogicIndexer.run()` on an identical fixture without an API key, and the
dumps of `files`, `symbols`, `summary_versions`, `retrieval_documents`,
`node_change_counters` plus all `RunResult` fields compared byte-for-byte
identical (timestamp columns excluded). Importing `llm_client` performs no
network I/O and creates no files; the pre-split `import run` baseline was
0.068 s (recorded only, no threshold).

In the same release the injection system converges on the MCP minimal view:
`generate_logic_tree_view` renders it unconditionally, the scope-selector
chain (`logic_scope_ui.py`, `remy-cc logic-scope`, selection files) is
removed, five injection env fields are dropped (registry 55 fields,
injection group 8), and stale keys in existing user configs round-trip
without activation. The installer installs the `mcp` package unconditionally
and aborts when pip fails or Python is older than 3.10. Verification has two
independent entry points that must agree on this contract: `install.py
--verify` (source checkout) and `cli.py::cmd_verify_runtime` behind
`remy-cc verify` (installed shim). Both report a missing `mcp` package as an
error and exit 1, and both require Python 3.10. (Historical since R4.4
Packet C: both v3 entry points retired; `remy-cc verify` is the single
entry, and the `mcp`-package probe retired with them — its production
consumer left the deployment set with the Python MCP server in R4.1.)

## query_impact rendering and counting

`_format_impact_result` labels each depth level with distinct file paths, so a
file that contributes several matched symbols is printed once instead of once
per symbol. The per-level `file(s)` and `symbol(s)` counts and the
`files affected` total are computed over the whole level. Previously the file
set accumulated over a `REMY_MCP_RESULT_LIMIT` prefix while the symbol totals
used the full list, so the two numbers in the summary line came from different
samples; `REMY_MCP_RESULT_LIMIT` no longer applies to this tool. Level labels
are capped at five files and the remainder is reported as `+N more file(s)`.

```
python -m pytest Remy-CC/tests/test_mcp_graph.py -k Impact -v
```

`TestQueryImpactRendering` covers label de-duplication, the per-level counts,
identical output across two result limits, the file total under a limit smaller
than the level's symbol count, the truncation marker, and its absence when every
file is shown.

## Summary invalidation scope

Summaries are invalidated only when structural identity changes.
`scanner.scan_file` marks a symbol summary `stale` when its hash changes; it
marks a file summary `stale` only when the file's symbol set changes
(`old_symbol_refs != new_symbol_refs`); `_detect_clusters` marks a cluster
summary `stale` only when the cluster's member set changes. Previously
`scan_file` marked the file summary `stale` unconditionally for every rescanned
existing file and cascaded to the cluster through
`mark_node_and_ancestors_stale`. Because
`summarizer._bump_parent_counter_if_applicable` skips the increment when the
parent summary is `stale` or absent, parents never entered
`collect_propagation_candidates` and `judge_propagation` was unreachable on the
content-change path. That function is removed; both call sites now use
`mark_current_summary_stale`.

```
python -m pytest Remy-CC/tests/test_struct_scan.py -k SummaryInvalidationScope -v
python -m pytest Remy-CC/tests/test_summary_versions.py -k ParentCounterBump -v
```

`TestSummaryInvalidationScope` drives real scans and covers: a body-only edit
keeps the file and cluster summaries usable, adding or removing a symbol
invalidates the file summary, and a body-only edit still invalidates the symbol
summary. Its assertions query the latest `summary_versions` row directly rather
than going through the module under test via `select_current_summary`.
`TestParentCounterBumpOnWrite` gains two cases: a `stale` parent summary does
not bump the counter, and the `stale` barrier blocks an older `ok` version — the
latter takes a different branch in `select_current_summary` than "parent has no
summary row at all".

One-shot verification on this repository's own index: before the change a scan
printed six zeros in `PROPAGATION_RESULT` and rebuilt 20 file and 4 cluster
summaries unconditionally; after the change a scan printed
`file_skip=1 cluster_propagate=1 cluster_skip=1`, added three LLM judgment rows
to `judge_cache`, sent only the 3 files whose symbol sets changed through
bootstrap, and reported 0 pending clusters. The two scans saw different change
counts (20 versus 3), so the call counts are not a cost comparison.

## Summary rewrite cost gating

`summarizer.write_summary_version` compares the incoming payload against the
immediately previous version's payload — the greatest version below the one being
written, regardless of that version's status — and skips the parent
`child_change_count` increment when they are equal. The row is still inserted and
`refresh_node` still runs, so versions stay monotonic and the projection stays
current. A missing predecessor, or one whose `summary` is NULL
(`status='pending'`), counts as a change.
`propagation.build_child_changes_payload` uses the same definition of "previous
version", so a predecessor already marked `stale` serves as the comparison
baseline instead of yielding `old_summary: null`. `run_propagation_pass` zeroes
the counter when `child_changes` is empty; `propagate=false` still keeps the
counter accumulating toward `REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY`.

```
python -m pytest Remy-CC/tests/test_summary_versions.py -k "ParentCounterBump or PromptExampleFieldContract" -v
python -m pytest Remy-CC/tests/test_propagation.py -k "BuildChildChanges or RunPropagationPass" -v
python -m pytest Remy-CC/tests/test_llm_judge.py -k PromptExampleFieldContract -v
```

`TestParentCounterBumpOnWrite` covers identical payload no-bump, differing
payload still bumps, version still increments when the bump is skipped, payload
equality across differing key order, a `pending` predecessor bumping again, a
`stale` predecessor with identical text not bumping, and the same gating on the
file-to-cluster edge. `TestBuildChildChanges` covers a `stale` predecessor used
as baseline, identical text across a `stale` predecessor returning an empty list,
and a `pending` predecessor yielding `old_summary: null`.
`TestPromptExampleFieldContract` (in both `test_summary_versions.py` and
`test_llm_judge.py`) parses the example payloads out of `summarize_file.md`,
`summarize_cluster.md` and `judge_propagation.md` and asserts their key sets are
subsets of what `_file_input`, `_cluster_input` and `build_prompt` actually emit.
All three templates previously documented field names that no caller passed.

Measurements on this repository's index that determined the comparison semantics:
all 57 stored version transitions have a predecessor whose status is `stale`
(symbol 34/34, file 18/18, cluster 4/4), because `scan_file` marks the current
summary `stale` before the rewrite. Restricting the lookup to
`status IN ('ok','oversized_warn')` therefore matches nothing — 0/57. Comparing
against the immediately previous version regardless of status matches 11/34 at
symbol level and 0/18 and 0/5 at file and cluster level, so the saving exists
only on the symbol-to-file edge.

Alpha normalization of symbol hashes was measured and rejected. Across the 39
commit pairs in the last 40 commits, 5473 symbol comparisons produced 426 that
the current hash treats as changed. Re-normalizing both sides through
`ast.unparse` matched 0 of those 426, and additionally rewriting local variables
and parameters to positional placeholders also matched 0. Controls confirm the
probe: a pure local rename, a parameter rename, a quote-style change and a
redundant-parens change were each detected, and a genuine `+1`-to-`+2` change was
not. `_calculate_symbol_hash` already strips all whitespace and `_strip_comments`
(replaced by `LanguageParser.symbol_hash_input` in R3.0a) already removes
comments, so no formatting-only class of change reaches the hash.

## P1.2.1 scan scope and parser cache identity

Schema 11.0.0 adds `parser_contract_version`, `parser_backend`, and
`parser_environment` to each `files` row. The 10.0.0 to 11.0.0 migration keeps
source hashes and structure facts, writes empty parser identities for legacy
rows, and lets the next scan replace each identity only after that file parses
successfully.

Incremental scan tests verify that configuration exclusions remove existing
facts and retrieval documents, excluded dirty paths are acknowledged, parser
contract or backend changes reparse only affected files, failed reparses keep
old facts and identities, and normalized incremental state matches a fresh full
scan. The TEE canary report includes the distinct parser cache identities stored
in the database for both regex and tree-sitter runs.

```bash
python -m pytest tests/test_struct_scan.py tests/test_migration_ladder.py tests/test_tee_canary.py -q -p no:cacheprovider
```

## P1.2.2 independent Remy configuration

Python runtime Remy settings use `~/.claude/remy-config.json` with optional
project overrides in `<project>/.claude/remy-config.json`. Tests cover source
precedence, strict schema/type validation, secret redaction, project-secret
rejection, companion locking, atomic replacement, one-time migration backups,
sentinel rejection, and simulated CC Switch rewrites of `settings.json`.

```bash
python -m pytest tests/test_remy_config.py tests/test_install_manifest.py tests/test_cli_manifest.py -q -p no:cacheprovider
```

The Windows CI job runs both `test_remy_config.py` and `test_index_state.py`.
P1.2.1 schema 11, parser identities, exclusions, and incremental/full-state
comparisons remain in the full regression suite.

## P1.2.3 Config UI behavior

The UI-A stage separates server configuration from unsaved drafts and defines
`reset_mode` as `none`, `non_secret`, or `all`. Tests verify sparse updates,
project overrides, secret preservation and explicit removal, unknown-field
preservation, invalid and mixed reset rejection, post-save refresh outcomes,
active-request cleanup, explicit shutdown, and disabled-control guards.
The Node tests execute the payload, actual-difference, and save-outcome state
functions extracted from `config_ui.html`.

```bash
python -m pytest tests/test_remy_config.py tests/test_config_ui.py -q -p no:cacheprovider
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m compileall -q remy-src tests
# Optional browser verification:
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python -m pytest browser_tests -q -p no:cacheprovider --browser chromium --tracing off --video off --screenshot off
```

UI-B1 adds an in-memory LLM endpoint test in global mode. The browser sends the
current API-key action, endpoint, and model to the local Python server without
saving them. The server issues one minimal chat-completions request with a
15-second timeout, no retries, default TLS verification, a 64 KiB local request
limit, and a 1 MiB upstream response limit. Redirects are not followed because
urllib otherwise preserves Authorization on redirected requests.

All POST routes require exact Host and Origin values, JSON content type, and a
process-scoped 256-bit session token. Each HTML response gets an independent
128-bit script nonce. Dynamic HTML and JSON responses use `no-store`,
`no-referrer`, and `nosniff`; HTML also applies a nonce CSP that denies framing,
forms, external scripts, and non-same-origin connections. Tests use unique fake
secrets and local fake services, reject real external requests, and assert that
secrets do not enter GET/test responses, errors, logs, or browser artifacts.
Playwright runs in a separate Ubuntu Chromium job and does not produce traces,
videos, or screenshots. The local server does not shut down when browser
heartbeats stop because browsers may suspend background tabs. It remains active
until the page Exit button calls `/api/shutdown` or the terminal receives
`Ctrl+C`; closing or minimizing the browser alone does not end the process.
JavaScript and Python strings cannot be proven erased
from process memory; the implementation only avoids persistence, limits copies,
and releases temporary payload references while preserving unsaved drafts.

The validated UI-A baseline is 604 passing tests with no Pyright errors or
warnings. A local Edge 151 run also verified that the initial Save button is
disabled, its disabled style is visible, and an unchanged Save attempt does not
produce an unsaved-changes prompt on exit. Browser automation, API endpoint
connectivity testing, responsive layout, motion, and accessibility remain in
the B1/B2 stages.

UI-B2-A restructures the configuration information architecture. The registry
declares bilingual labels, optional paired units, an `advanced` flag, and a
four-value `restart_scope` (`immediate` / `next_index` / `next_session` /
`next_mcp_launch`) for every field. The seven display groups are
`llm_api / index_generation / injection / mcp / summary / timeline / system`
with field counts 7/12/13/6/12/2/6 (58 total, unchanged keys, schema 1.0.0).
The page separates the `#remy-host` header (title, mode, language, exit) from
the `#config-page` content (search, group navigation, fields, sticky action
bar). Desktop uses a group sidebar; below 900 px a native select replaces it.
Only seven fields are common; the rest fold into per-group advanced sections,
and modified or pending-restore fields stay pinned. Search runs locally with
normalized Unicode-lowercase substring matching over keys, bilingual labels,
descriptions, and group names, keeps registry order, locks matched groups
expanded, and returns focus to the search box on Escape or clear. Global
single-field restore uses the new strict `/api/save` `remove_keys` contract:
a duplicate, unknown, secret, project-mode, or reset-mixed request returns
HTTP 400 with unchanged file bytes. Project restore keeps the overrides-diff
path. The B1 connection test moved to an LLM-group-level control with its
request, security, and lifecycle contracts unchanged.

The validated B2-A baseline is 648 passing tests in the full suite (six new:
registry metadata and FieldSpec validation in `test_remy_config.py`;
`remove_keys` acceptance/rejection, GET metadata, and the search/state/restore
Node functions in `test_config_ui.py`) plus 8 Playwright Chromium tests (four
new: desktop search/navigation/advanced folding at 1280×800, the group select
at 390×844, the single-field restore round trip including edit-cancels-restore,
and saving a modified field hidden by an active search). The registry test
asserts the exact field-to-group assignment for all 58 fields and the
documented `restart_scope` values of representative consumers. Both Pyright
runs report zero errors and zero warnings.

## Call-edge resolution: form downgrade and import supplements

Schema 12.0.0 adds `call_form` to `edges` (`name` / `attribute`, default `name`)
and `import_bindings` to `files` (import bindings the parser could not map to a
project file, as a JSON list). The Python parser distinguishes `ast.Name` from
`ast.Attribute` calls and collects unresolved import bindings; the C/C++ and
TypeScript parsers are unchanged and keep both defaults. The 11.0.0 to 12.0.0
migration adds the columns idempotently; on ladders starting before `edges`
existed the handler skips that ALTER and `SCHEMA_SQL` later creates the table
with the new column in place.

`_resolve_call_edges` derives two data sets from all stored `import_bindings`
on every postprocess pass, in memory and independent of file scan order: a
unique path-suffix match of the module name against indexed Python files
(`pkg/mod.py` or `pkg/mod/__init__.py`; multiple hits are inconclusive) becomes
an import-layer supplement, and a miss (stdlib modules short-circuit via
`sys.stdlib_module_names`) marks the bound names as external. A bare-name
callee found in the external set skips the global fallback. Attribute calls
resolved at the import or global layer are downgraded to `speculative`;
same-file hits are exempt, so `self.method()` calls stay `definite`. Downgraded
single-candidate edges are not written to `edge_candidates`.

```
python -m pytest Remy-CC/tests/test_struct_scan.py -k ResolveCallEdges -v
python -m pytest Remy-CC/tests/test_migration_ladder.py -k v11_to_v12 -v
```

Verification record (2026-08-08, both implementations full-scanning the same
97-file corpus):

- Provenance distribution: definite 1366→1706, probable 1543→101, speculative
  223→1322, unresolved 4854→4857. The same-name false edge from
  `test_mcp_minimal.py` to `patch_descriptions.py::patch` went from `probable`
  to unbound.
- Pyright full-graph ground truth (one `LspClient` session from `eval/gen_gt.py`
  harvesting 93 files, 708 callers, 1521 in-project edges in 47.8 s with zero
  errors; a one-off probe per the A1.1 precedent, no permanent harness):
  static-only precision (definite+probable) 0.712→0.919; all-resolved precision
  0.691→0.693 and recall 0.951→0.952. 109 true attribute-form calls (e.g.
  `remy_config.load_config`) moved into `speculative` under the downgrade rule
  while keeping the correct target. The remaining `probable` layer (40
  GT-decidable bare-name global single candidates) measured 0.025 precision.
- No query-layer changes: the flow labels `call [name-match]` and
  `call [speculative resolution]` and their tests predate this work (commit
  `ad687885`), and the `static_only` filter `IN ('definite','probable')` is
  unchanged, so downgraded edges drop out of static-only output automatically.
- TEE canary: both fixture backends and their fixed assertions show no
  regression; `call_form` stays at the `name` default for C/C++ edges.

## Public TEE canary

The committed fixture comes from `openharmony-sig/tee_tee_os_framework` commit `b11ffb19d83da42047cc0b5cbfbbfb95ba3304f4` under MulanPSL-2.0. Its manifest records each copied file's Git blob SHA. The fixture retains the upstream license and source headers. CI does not access the network.

The tree-sitter run asserts known symbols, a direct `handle_ns_cmd -> dispatch_ns_cmd` edge, and the inferred `dispatch_ns_global_cmd -> need_load_app` edge. The regex fallback does not extract direct C call edges; it still asserts symbols, function-pointer facts, and the inferred edge.

Run the fixed full project locally from a Git checkout at the recorded commit:

```bash
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend tree-sitter --scope product --output tee-product.json
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend tree-sitter --scope full-tree --output tee-full-tree.json
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend regex --scope product --output tee-regex.json
```

The full-project command rejects non-Git directories and revisions other than the recorded commit. It archives the committed tree into a system temporary directory before scanning, so it does not create a database in the input checkout. The JSON report contains the parser backend, scope, source revision, scan status, file/symbol/pattern/direct-edge/inferred-edge/function-pointer-edge counts, pattern counts by type, all file-and-type pattern sources, elapsed seconds, database bytes, and WAL bytes. The manifest can declare forbidden pattern facts for the fixed revision; P0.6 uses this to reject `c_fnptr_register` entries for the `pic1080s`, `pic1440s`, and `back_png` byte arrays. Timing and storage fields are observations, not pass/fail thresholds.

## PreToolUse guard

`hooks/pre_tool_guard.py` decides whether each Write / Edit / Bash / PowerShell /
Agent call is allowed, rewritten, or denied. It had no behavioural coverage until
this suite: the only prior reference was `test_install_manifest.py` asserting that
its path appears in the install manifest.

```
python -m pytest Remy-CC/tests/test_pre_tool_guard.py -v
```

Pure helpers are exercised in-process through an `importlib` load of the hook
module. The `main()` decision matrix is exercised by piping a JSON payload to the
script through `subprocess` and parsing `hookSpecificOutput`. The subprocess
environment overrides `HOME` / `USERPROFILE` to a temporary directory and sets
`REMY_LANG=en`, so no test reads the developer's real `remy-config.json`.
Assertions target `permissionDecision`, `updatedInput` and `additionalContext`
only — never message wording — so they hold under either language.

Sixteen `main()` branches are covered: Bash with and without an existing
encoding/miniforge marker; PowerShell with and without a Python command; the Plan
agent language injection; the Explore / general-purpose confirmation gate; an
unknown agent type falling through silently; Write / Edit / NotebookEdit denial on
unconfirmed evidence; Write allowed on confirmed evidence; an absolute path inside
the project rewritten to relative; an absolute path outside the project asking for
confirmation; Read / Glob / Grep exempt from that gate; the three kebab-case
outcomes (deny on a new file, redirect when only the snake variant exists, ask when
both exist); the Edit soft reminder; the lock-file warning; a payload without any
path exiting silently; and malformed stdin failing open with a stderr note.
The packet gate additionally covers the `.claude/temp_task/` exemption (a
relative path, an absolute path, a new file, and a `.claude/` path outside
`temp_task` remaining gated) and a structurally invalid packet denying writes
to project files.

Four defects were originally recorded here as `xfail(strict=True)` cases.
Three are fixed and one was ruled intentional; all four are regular regression
tests now:

| Case | Resolution |
| :--- | :--- |
| `test_does_not_reinject_when_encoding_already_set` | Fixed. `inject_bash_env` skips any command carrying the injected preamble marker (`.env_setup.sh`) and adds the encoding export only when the command does not already set it. A command that sets the encoding manually still receives the mamba preamble once. |
| `test_evidence_entry_missing_id_is_rejected` | Fixed. `validate_packet` validates structure explicitly and fails closed: invalid JSON, a non-object root, non-list `evidence`/`proposed_changes`, a non-dict entry, a missing or non-string `id`, non-list `evidence_refs`, and a non-string ref all deny with a remediation hint. Only I/O errors still fail open. |
| `test_writing_to_the_active_packet_is_permitted` | Fixed. `main()` exempts write targets under `.claude/temp_task/` from the evidence gate, so the remediation the gate demands — promoting evidence to `confirmed` or repairing a malformed packet — is always executable with Edit/Write. This exemption and the fail-closed change above landed together; either alone would deadlock or stay silently bypassable. |
| `test_bash_bypasses_packet_validation_by_design` (renamed from `test_bash_is_subject_to_packet_validation`) | Not fixed — ruled intentional. A shell command cannot be statically classified as read or write, and skill protocols write into `.claude/temp_task/` through Bash. The rationale is recorded in the case's docstring, and the bypass is asserted as an `allow`. |

`test_validation_is_not_scoped_to_a_file_path` still records that the gate is
global: one unconfirmed reference anywhere in `proposed_changes` blocks edits
to every file outside `.claude/temp_task/`. Narrowing per file would require a
change-to-file mapping that the packet schema does not carry.

Assertion strength was checked by mutation in a temporary copy of the tree:
the four original mutations (dropping the suspected/stale check, dropping the
Python detection in `inject_bash_env`, widening `has_kebab_case` to the whole
path, and replacing the `commonpath` check in `path_is_contained` with a
string prefix) each broke exactly one assertion. Four post-fix mutations
(removing the temp_task exemption, swallowing structural errors again,
dropping the export precondition, and dropping the preamble-marker skip) broke
3, 2, 1 and 2 corresponding assertions against a green 72-case baseline.

### Hook and installer module coverage

The three modules previously listed here as having no behavioural coverage
gained tests; two of the suites remain since R4.3:

```
python -m pytest Remy-CC/tests/test_enforcer_hook.py Remy-CC/tests/test_patch_descriptions.py -v
```

**Removed (R4.3)**: `tests/test_logic_dirty_tracker.py` retired together with
the dirty-queue hook (`9d1d3e8`); the PostToolUse dirty path is now
`remy-cc hook dirty` over IPC, covered by `tests/test_daemon_ipc.py`.

`tests/test_enforcer_hook.py` copies the hook into a temporary directory to
control the reminder file set: `REMY_LANG=zh-CN` selects
`reminder_prompt_zh.md`, `en` (or an unset language) selects the English file,
a missing primary falls back to the other language, the documented default
string is returned when both files are absent, and the loaded text is
stripped. `remy-src` is supplied through `PYTHONPATH`.

`tests/test_patch_descriptions.py` covers `patch()` in-process: the
`description:` line is rewritten only within the first `MAX_FRONTMATTER_LINES`
lines, the requested language falls back to `en`, an unchanged line does not
write the file (proven with a read-only target file), and a missing or
malformed `skill_descriptions.json` warns on stderr without touching any
SKILL.md.

`remy-src/patch_descriptions.py` appears in the logic index as being called
from `test_mcp_minimal.py`; that edge is a same-name collision with
`unittest.mock.patch` and predates the real coverage above.

## R2.4 installer transaction matrix

R2.4 moves install ownership into `remy-src/install_runtime/`. Tests use explicit
`CLAUDE_CONFIG_DIR`, `REMY_CC_HOME`, `HOME`, and `USERPROFILE` values under the
system temporary directory. No install, upgrade, rollback, or uninstall test
uses the developer's real user directories.

The matrix covers manifest v1/v2 migration to v3, Python and Rust Hook modes,
repeated installation, unknown targets, modified managed files and settings
claims, corrupt manifest and transaction metadata, strict transaction/runtime
fields, daemon running/unknown rejection, stale unheld lock handling,
same-version/different-hash daemon rejection, pre-commit rollback, the
manifest-publication crash window, committed cleanup recovery, atomic
uninstall manifest removal, default state preservation, explicit state purge,
project-index preservation, spaces and non-ASCII root paths, exclusion of
Python cache files from deployment, preservation of pre-existing settings
permissions, stable JSON results, and exit codes 0 through 4.

```bash
python -m pytest Remy-CC/tests/test_install_manifest.py Remy-CC/tests/test_cli_manifest.py Remy-CC/tests/test_cli_daemon.py Remy-CC/tests/test_daemon_ipc.py -q -p no:cacheprovider
pyright -p Remy-CC/pyrightconfig.json
cargo fmt --check --manifest-path Remy-CC/remy-cc/Cargo.toml
cargo clippy --workspace --manifest-path Remy-CC/remy-cc/Cargo.toml --all-targets -- -D warnings
cargo test --workspace --manifest-path Remy-CC/remy-cc/Cargo.toml
```

The 2026-08-13 Windows verification passed 130 targeted Python tests with 1
skip, 957 full Python tests with 3 skips, 60 Rust unit tests, 13 Rust CLI
integration tests, and Pyright with zero errors and warnings. The known
`IncompleteFieldDefinitionWarning` from the MCP dependency remains unchanged.
A `crt-static` release build reported `remy-daemon 0.2.0` and contained no
`VCRUNTIME140` byte string. A temporary-directory end-to-end probe verified
Rust-mode install, verify, default uninstall, reinstall, `--purge-state`, and
project-index preservation. The unchanged browser UI suite passed 8 Chromium tests.

## F.1 incremental postprocess (scanner-core 0.2.0)

The Rust `scan_files` path replaces the global postprocess with
`postprocess::run_incremental`: the direct-edge reset covers only the
affected edge set (edges of touched files ∪ callee in the old∪new name
superset of touched files ∪ edges of import-binding hosts touched by .py
additions/removals; old callee_file captured before the reset, matching
edge_candidates deleted with it), purge/synth/trait-bases stay global but
are bracketed by `(source_file, callee_file, callee_qualified)` count
snapshots, and the diff endpoints plus the direct-edge old/new endpoints
drive targeted kind_hint (per file) and cluster (per top-level group)
recomputation. `scan_full` keeps the global `run()`. Equivalence gate:
incremental and full-rescan VIEWS states are byte-identical (clusters,
edge_candidates, and retrieval_documents included), locked by:

- `scan.rs` inline tests: a perturbation sequence (rename / add / delete)
  compared step-by-step against full rescans over an extended projection
  including kinds, clusters, and candidates; two-file scan-order
  commutativity; zero edge_candidates orphans (`edges.id` is AUTOINCREMENT
  and the connection never enables foreign_keys, so the write layer
  deletes candidates before their edges).
- `test_scanner_core_diff.py`: perturbation sequences with zero blocking
  findings over the full views, an orphan regression, and a fanout-cap
  overflow case (`REMY_SYNTH_EVENT_FANOUT_CAP=1`: a delta file pushes an
  observer signal past the cap, dropping an inferred edge between two
  non-delta files and sinking the pkg cluster below the density
  threshold — exercising snapshot-diff coverage of non-delta endpoints).
- `test_postprocess_parity.py`: summary/retrieval state parity with the
  Python oracle along the incremental path.

Registered note: the python fallback arm keeps `REMY_STRUCT_SCAN_TIMEOUT`
(default 60 s; the gpu corpus measured 65.3 s) for incremental jobs — raise
it per repository size before switching back.

## C2 docstring hash exclusion (contract version 3)

The C2 ruling (docs/RETIREMENT.md §4) removes the Python docstring literal
from the symbol hash input on both implementations. The parser locates the
docstring node (ast / tree-sitter) and splices its byte span out of the
symbol's segment into `SymbolInfo.hash_source_segment`; the hash consumers
(`scanner.scan_file`, Rust `parse_one` and `write_file_facts`) hash
`hash_segment()` instead of `source_segment`. `source_segment` itself is
unchanged — LLM summarization and the filter-small line count still see the
full text. `CACHE_CONTRACT_VERSION` bumped 2 → 3 on both sides; existing
databases re-parse `.py` rows via the identity-invalid path on first scan.

Equivalence gate: both implementations produce byte-identical hash inputs.
Locked by:

- `test_struct_scan.py::TestDocstringExcludedFromHash`: docstring-only edits
  keep the hash and body edits change it across four literal styles (plain,
  `#` inside the docstring, raw single-quoted, class docstring); a
  triple-quoted assignment value stays inside the hash; symbols without a
  docstring carry `hash_source_segment=None`.
- `parse_python.rs` inline tests: splice correctness, CPython
  constant-folding parity for concatenated plain literals, rejection of
  f-string/bytes/assigned first statements, per-symbol class/method removal.
- `test_scanner_core_diff.py::test_docstring_hash_exclusion_matches_across_implementations`
  and `::test_docstring_only_edit_is_hash_neutral_in_rust`: identical
  `symbols.hash` values across implementations over a mixed corpus, and
  hash neutrality of docstring-only edits on the Rust arm.

Known asymmetry (registered, not a gate): CPython folds adjacent plain
string literals into one `Constant`, so a concatenated docstring is removed
on both sides (`concat.py` in the diff corpus); f-strings and bytes
literals are never docstrings on either side.

## Rust import probe case check (rust parser contract version 4)

`resolve_imports` maps `use`/`mod` heads to project files by an existence
probe; on case-insensitive filesystems (Windows/macOS) that probe matched
entries regardless of case, so `use super::Clock` inside `clock.rs` recorded
a dangling self-import `Clock.rs` that never appears on Linux. Both sides
now verify every source-derived segment against the on-disk entry name
(`_case_exact_on_disk` in `rust_parser.py`, `case_exact_on_disk` in
`parse_rust.rs`) after the existence hit; a case mismatch counts as absent
and the k-shrinking probe loop continues. Case-sensitive filesystems are
unaffected (an existence hit is always an exact match there). The rust
parser `CACHE_CONTRACT_VERSION` bumped 3 → 4 on both sides so existing
`.rs` rows re-parse via the identity-invalid path and stale wrong-case
entries drop out. Locked by
`test_rust_parser.py::TestRustImports::test_use_super_type_name_does_not_self_import`
/ `test_mod_declaration_case_mismatch_is_not_recorded`, the
`case_mismatched_probes_are_rejected` inline test in `parse_rust.rs`, and
the `casing.rs` corpus fixture in the cross-implementation diff suite.

## Language-bounded global resolution (all parser contracts +1)

The direct-edge resolver's global same-name tier matched symbols regardless
of source language, so a bare Python call like `dirname` could resolve to a
Rust `fn dirname` (a workspace probe on 2026-08-26 counted 986 such
cross-language edges, 12.9% of resolved edges; the index does not model
cross-language FFI, so every one is wrong). Both sides now join `files`
in the global tier and require `files.language` equality with the caller's
file; same-file and import tiers are inherently same-language and are
unchanged. Because the Rust incremental postprocess only re-resolves
targeted edges, already-resolved rows in deployed databases would never
heal on their own — all four parser `CACHE_CONTRACT_VERSION`s bumped by one
(python 3 → 4, c_cpp 1 → 2, ts 2 → 3, rust 4 → 5, both sides in lockstep)
so every stored row re-enters the identity-invalid path and the full edge
set re-resolves under the new rule on the first scan after upgrade.
Symbol content hashes are unchanged, so summaries do not regenerate.
Locked by
`test_struct_scan.py::TestResolveCallEdges::test_global_tier_skips_cross_language_candidates`
/ `test_global_tier_same_language_candidate_wins_over_cross_language`, the
`global_tier_is_language_bounded` inline test in `postprocess.rs`, and the
`cross_lang.py` / `cross_lang.rs` corpus pair in the cross-implementation
diff suite.

## state.db WAL backup constraint

`~/.remy-cc/state.db` runs in WAL journal mode. Rows not yet checkpointed live
only in `state.db-wal`, so copying the main database file alone silently drops
them. A workspace probe on 2026-08-21 confirmed the failure mode: the real
state database held 47 jobs, all of them still in the WAL, and a bare copy of
`state.db` produced a database with 0 jobs.

Any snapshot of a live state database must therefore use one of:

- the SQLite backup API (`rusqlite` `backup` feature, `sqlite3 ".backup"`, or
  Python `sqlite3.Connection.backup`), which folds WAL content into the copy; or
- a file-level copy that includes `state.db-wal` and `state.db-shm` alongside
  the main file, taken while no writer holds the database.

The daemon's built-in v1→v2 migration uses the backup API for its pre-migration
`state.db.bak`, so the shipped path is unaffected. The constraint applies to
manual snapshots, test fixtures, and any future tooling that copies a live
state database.

## R4.2 schema owner and the frozen Python ladder

R4.2 ruling (authoritative text: RETIREMENT §2.5): the Rust owner
(`writer.rs::open_db`) supports the current schema version only. The five-state
dispatch matrix is covered at two layers — `test_schema_rebuild.py` (CLI
end-to-end, skipped without a built binary) and the `writer.rs` unit tests:

- below current, or tables present without a version row → backup to `.bak`
  via the backup API, then rebuild; an incremental entry escalates to the full
  file set inside the same locked call (`SCHEMA_REBUILD` stderr notice; the
  scan_result v1 line is unchanged);
- equal to current → idempotent DDL replay, zero data writes, no `.bak`;
- above current, or unparseable version string → refused unchanged
  (byte-identity assertion).

Window discipline (until the R4.3 Python exit):

1. The ladder in `migrations.py` is **frozen** — no edits to existing
   segments. The single exception is a schema bump shipped inside the window:
   the Python side adds the segment plus its `test_migration_ladder.py` case
   matrix (idempotent re-entry, rollback on failure, migration_log record);
   the Rust rebuild floor tracks `SCHEMA_VERSION` automatically and needs no
   change.
2. Known behavioral difference between the arms: Rust rebuilds below-current
   databases (self-healing; `summary_versions` content stays in `.bak` only),
   while the Python ladder migrates 6–11 losslessly and refuses <6. Use the
   Python arm (`struct_scan.py`) when a lossless migration is required. The
   difference converges at R4.3.
3. Resident-state sample factories are single-sourced in
   `tests/ladder_samples.py`, shared by the ladder tests and the rebuild
   tests; no pre-generated database binaries are committed.

**Window closed (R4.3)**: the ladder, `migrate_json`, and their suites
(`test_migration_ladder.py`, `ladder_samples.py`) are deleted.
`initialize_database` now refuses every non-current schema version with the
database preserved and points at `remy-cc scan` as the schema owner;
`tests/test_initialize_database.py` asserts the refusal against the frozen
DDL snapshots in `tests/schema_snapshots.py` (v6/v7/v10, iterdump-identical
before and after), and `tests/test_run_routing.py` covers the run.py
routing that spawns `remy-cc scan` for the structural segment and gates
the semantic segment on `schema.VERSION`. The dirty queue, its hooks
(`logic_dirty_tracker.py`, `logic_enrichment_hook.py`), the
`--consume-dirty`/`--worker-config-json` arms, and the daemon's python
provider arm are deleted in the same batch; the daemon suites run rust-only
(`test_daemon_provider.py`), hook IPC failures emit diagnostics instead of
falling back, and enrichment stays available without a running daemon
(INV-R1, `test_daemon_ipc.py`).

## query_dependencies dedicated suite (Rust single-implementation)

`tests/test_mcp_dependencies.py` is the acceptance surface for the
`query_dependencies` MCP tool, which has no Python oracle arm and is excluded
from the H.4 differential matrix (MCP_RUST_PARITY_BASELINE.md §4.2). It drives
the release `remy-cc mcp` binary (skipped when not built) against a
purpose-built corpus and asserts: stored-plus-derived edge merging with a
golden rendering, unique-suffix derivation (stdlib short-circuit, multi-hit
drop), the `(not indexed)` dangling marker, up/down duality, cycle
termination, depth clamping to `REMY_MCP_BFS_MAX_DEPTH`, the invalid-direction
error, byte-identical repeated calls, and an unchanged DB snapshot hash.

```bash
python -m pytest Remy-CC/tests/test_mcp_dependencies.py -v
```

## R4.4 segment 1: self-install module (Rust)

The `src/install/` module family ships its own unit surface inside the crate
(part of `cargo test --workspace`): archive reconciliation against an
independent source-tree enumeration plus ignore/safety/size invariants
(`embedded`), the settings merge family behaviorally ported from
`install_runtime/settings.py` with verbatim error texts (`settings`), v4
manifest validation with read-only v3 parsing (`manifest`), pending-deletes
v1 interop (`pending`), the install lock (`lock`), canonical JSON parity —
the workspace compiles serde_json with `preserve_order`, so key sorting is
explicit (`storage`) — and the install/verify/uninstall operations
end-to-end in temp roots including v3 migration and legacy hook clearing
(`ops`, `update`). `tests/daemon_cli.rs` adds `restart`/`logs` integration.
The release-binary probe matrix (temp roots, 12 items) covers fresh install,
clean verify, idempotent rerun, drift and tamper rejection, interrupted-run
convergence, daemon start/restart/logs/stop on the deployed binary,
default-uninstall preservation (project data, user config, engine state),
rename-aside deferral of the running image with a later pending sweep, v3
manifest migration (shim and pre-rename binary removed), and
`--purge-state`.

### Install conflict resolution (2026-09-04 batch)

The preflight ownership check collects the full unmanaged-conflict list on
`InstallError.conflicts` instead of failing at the first hit. Unit surface
(in `ops` and the new `legacy` module, same crate test run): multi-conflict
collection with a zero-modification guarantee on the error path; approved
overwrites writing a byte-identical `.bak` before deployment; refusal of an
approved overwrite whose disk hash drifted between runs; the legacy
`.installer_manifest.json` hash gate (byte-identical records deletable;
hash-mismatch / no-hash / outside-claude-root / `..` / missing records
retained with reasons); v1 absolute and v2 relative path shapes; corrupt or
unshaped legacy manifests erroring without touching disk; execute() pruning
emptied directories, dropping settings.json hook entries that reference a
deleted script (emptied events removed), and renaming the manifest to
`.bak`; post-commit renaming (not deleting) a leftover legacy manifest.
Manual acceptance scenarios (E1-E8): refuse prompt 1 / refuse prompt 2
(zero modification, full report, exit 1), conflicts without a legacy
manifest (prompt 2 only), corrupt legacy manifest (warn, prompt 2 path),
`--non-interactive` or non-TTY stdin (report and exit 1, no prompts),
between-run drift abort, identical-content adoption (no prompt, unchanged),
and a v3/v4 upgrade with a leftover legacy manifest (renamed to `.bak`
post-commit).

## Boundaries

Committed tests use synthetic source or the fixed MulanPSL-2.0 TEE fixture, temporary directories, and temporary SQLite databases. They do not require an LLM API key or network access. P0.3 compares normalized full and incremental states. P0.4 adds fixed-revision symbols and relationships, repeated full-scan idempotency, handler rename/delete comparisons, parser-backend reporting, and local full-project measurement commands. P0.5 moves the structural implementation into `schema.py`, `symbol_names.py`, `migrations.py`, and `scanner.py`; `struct_scan.py` remains the stable CLI/import entry point. P0.6 rejects scalar and byte arrays before emitting positional registration facts, rejects numeric and expression handler values, preserves Unicode word identifiers, reports pattern types and sources, and checks the three known image arrays in the fixed full project. The fixed project has no known function-pointer struct table that omits inner aggregate braces; that C form remains outside the verified parser contract. Migration tests import without parser modules, while the full suite, Pyright, compatibility exports, both fixture backends, and the three fixed full-project scans verify the current behavior.
