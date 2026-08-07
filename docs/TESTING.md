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

CI runs the full suite on Python 3.10 without tree-sitter and Python 3.12 with the pinned tree-sitter packages. Both jobs run the fixed public TEE fixture canary with their available parser backend. A Windows Python 3.12 job executes the process-lock and dirty-queue tests.

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
unchanged. The default LLM channels in `index_mcp_queries.py` and `cli.py`
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
--verify` (source checkout) and `cli.py::cmd_verify` behind `remy-cc verify`
(installed shim). Both report a missing `mcp` package as an error and exit 1,
and both require Python 3.10.

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
python -m pytest Remy-CC/tests/test_mcp_queries.py -k Impact -v
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
already removes comments, so no formatting-only class of change reaches the hash.

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

Four cases assert the expected post-fix behaviour and carry
`pytest.mark.xfail(strict=True)`, so fixing the defect turns them into XPASS
failures that prompt removing the marker:

| Case | Defect |
| :--- | :--- |
| `test_does_not_reinject_when_encoding_already_set` | The skip condition at `inject_bash_env` requires BOTH `PYTHONIOENCODING` and `miniforge3`, so a command that already sets the encoding is injected a second time (measured: the export appears twice). |
| `test_evidence_entry_missing_id_is_rejected` | An evidence entry without an `id` key raises `KeyError` in the dict comprehension; the broad `except` swallows it and `validate_packet` returns `(True, None)`, so a malformed packet permits every write. |
| `test_bash_is_subject_to_packet_validation` | The Bash branch exits before `validate_packet` runs, so `echo x > f` writes files while evidence is still unconfirmed. |
| `test_writing_to_the_active_packet_is_permitted` | The guard denies writes to the packet itself, so the remediation it demands — promoting evidence to `confirmed` — cannot be performed with Edit or Write. |

`test_validation_is_not_scoped_to_a_file_path` records a fifth issue without an
xfail marker, because the current behaviour is what the assertion states: a single
unconfirmed reference anywhere in `proposed_changes` blocks edits to every file,
since `validate_packet` takes no `file_path` argument.

Assertion strength was checked by mutation: dropping the suspected/stale check,
dropping the Python detection in `inject_bash_env`, widening `has_kebab_case` to
the whole path, and replacing the `commonpath` check in `path_is_contained` with a
string prefix each broke exactly one assertion and no others.

### Modules still without behavioural coverage

Recorded so the gap is explicit rather than rediscovered:

| Module | Lines | Only reference | What is untested |
| :--- | :--- | :--- | :--- |
| `hooks/logic_dirty_tracker.py` | 58 | `test_cli_manifest.py` manifest hash entry | PostToolUse dispatch: the non-Edit/Write early exit, the missing-`file_path` early exit, `DirtyQueue.record` delegation, and the bare `except` that swallows every failure |
| `hooks/env_system/enforcer_hook.py` | 59 | none | `load_reminder_text` language selection, the primary-to-fallback file order, and the "files missing" default string |
| `remy-src/patch_descriptions.py` | 66 | none | `patch()` frontmatter rewriting within `MAX_FRONTMATTER_LINES`, the `lang` to `en` fallback, the unchanged-line short circuit, and the missing-file / malformed-JSON warnings |

`remy-src/patch_descriptions.py` appears in the logic index as being called from
`test_mcp_minimal.py`; that edge is a same-name collision with
`unittest.mock.patch` and not real coverage.

## Boundaries

Committed tests use synthetic source or the fixed MulanPSL-2.0 TEE fixture, temporary directories, and temporary SQLite databases. They do not require an LLM API key or network access. P0.3 compares normalized full and incremental states. P0.4 adds fixed-revision symbols and relationships, repeated full-scan idempotency, handler rename/delete comparisons, parser-backend reporting, and local full-project measurement commands. P0.5 moves the structural implementation into `schema.py`, `symbol_names.py`, `migrations.py`, and `scanner.py`; `struct_scan.py` remains the stable CLI/import entry point. P0.6 rejects scalar and byte arrays before emitting positional registration facts, rejects numeric and expression handler values, preserves Unicode word identifiers, reports pattern types and sources, and checks the three known image arrays in the fixed full project. The fixed project has no known function-pointer struct table that omits inner aggregate braces; that C form remains outside the verified parser contract. Migration tests import without parser modules, while the full suite, Pyright, compatibility exports, both fixture backends, and the three fixed full-project scans verify the current behavior.
