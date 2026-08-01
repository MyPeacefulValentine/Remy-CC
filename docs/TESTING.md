# Testing

## Environment

Remy-CC supports Python 3.10 or newer. Install the pinned development tools with:

```bash
python -m pip install -r requirements-dev.txt
```

Install the optional high-precision parser packages with:

```bash
python -m pip install -r requirements-tree-sitter.txt
```

## Verification baseline

The repository baseline before P0.1a/P0.1b implementation contained 454 collected tests. New tests may increase this number.

```bash
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m pytest tests/test_struct_scan.py tests/test_enrichment_hook.py tests/test_index_state.py -q -p no:cacheprovider
python -m pytest tests/test_migration_ladder.py -q -p no:cacheprovider
python -m pytest tests/test_synthesizers.py tests/test_c_fnptr_dispatch.py -q -p no:cacheprovider
python -m pytest tests/test_struct_scan.py tests/test_fts_three_layer.py -q -p no:cacheprovider
```

CI runs the full suite on Python 3.10 without tree-sitter and Python 3.12 with the pinned tree-sitter packages. A Windows Python 3.12 job executes the process-lock and dirty-queue tests.

## Boundaries

All committed tests use synthetic source, temporary directories, and temporary SQLite databases. They do not require an LLM API key or network access. P0.3 compares normalized full and incremental states and records global direct-edge resolution and synthesizer timings. The public TEE repository scan is not part of this CI baseline. P0.4 will fix its repository revision, exclusion rules, and expected measurements.
