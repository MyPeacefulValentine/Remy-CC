# C task group (P2.0 first batch)

Eight tasks over the in-repo C corpus `tests/fixtures/tee_canary` (4 files, Mulan PSL v2).
Covers §9.2 groups: C direct calls (c01-c04), C function pointers (c05-c07), and one
conditional-compilation probe (c08). The C macro group is NOT covered here: tee_canary
contains `tloge` call sites but not the `tee_log.h` macro definitions, so macro-body
hidden-call tasks require the full external tee corpus (registered as known-invisible
for the tree-sitter arm in the evolution plan §9.2).

## Ground truth

GT is human-verified against source (2026-08-22); each task's `gt_source` records the
verified line anchors. This is the manual-GT bootstrap path registered in the evolution
plan P2.0 — pyright covers Python only (`build_tasks.py`), and no compiler-backed C GT
channel exists yet. Re-verify anchors after any tee_canary fixture change.

Seed provenance: probe measurements in the workspace report
`.claude/temp_log/longterm_investigation_20260822_p2p6.md` (macro second-hop 0 edges,
fnptr dispatch coverage 60.6% on the full tee corpus).

## Running

The default scoped DB (`eval/.scoped/logic_index.db`, 45 files) excludes C sources.
Build a dedicated DB that includes tee_canary, then point the CLI at it:

```bash
# from the workspace root (do not cd into Remy-CC/)
remy-cc scan --root Remy-CC --db Remy-CC/eval/.scoped/c_logic_index.db --result-json
PYTHONPATH=Remy-CC python -m eval.cli --tasks Remy-CC/eval/tasks/c \
    --db Remy-CC/eval/.scoped/c_logic_index.db --reps 3 --save
```

All `file` and `expected` paths are Remy-CC-repo-relative (the default `--target`),
matching the python task group convention.

## Expected-set conventions

- Tasks asking for definitions restrict the answer set to functions DEFINED inside the
  fixture tree; handlers/callees declared in headers but defined outside (e.g.
  `register_agent`, `set_service_thread_cmd`) are excluded by the prompt text.
- c05/c07 are the dispatch-visibility probes: `need_load_app` is reachable only through
  `g_ns_sync_cmd_table` (tee_ns_cmd_dispatch.c:72-90) and the dispatch loop
  (`dispatch_ns_global_cmd`, :105-108). A retrieval arm without function-pointer
  synthesis is expected to miss these; that gap is the measurement target, not a task
  defect.
