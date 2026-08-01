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
thirty measured iterations. Schema remains 10.0.0 and no migration is applied.

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

## Boundaries

Committed tests use synthetic source or the fixed MulanPSL-2.0 TEE fixture, temporary directories, and temporary SQLite databases. They do not require an LLM API key or network access. P0.3 compares normalized full and incremental states. P0.4 adds fixed-revision symbols and relationships, repeated full-scan idempotency, handler rename/delete comparisons, parser-backend reporting, and local full-project measurement commands. P0.5 moves the structural implementation into `schema.py`, `symbol_names.py`, `migrations.py`, and `scanner.py`; `struct_scan.py` remains the stable CLI/import entry point. P0.6 rejects scalar and byte arrays before emitting positional registration facts, rejects numeric and expression handler values, preserves Unicode word identifiers, reports pattern types and sources, and checks the three known image arrays in the fixed full project. The fixed project has no known function-pointer struct table that omits inner aggregate braces; that C form remains outside the verified parser contract. Migration tests import without parser modules, while the full suite, Pyright, compatibility exports, both fixture backends, and the three fixed full-project scans verify the current behavior.
