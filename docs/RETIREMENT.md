# Retirement Register (R3.6 Audit Record)

Audit date: 2026-08-23. Anchor commit: `c84e90e`. Authority: this file records the
R3.6 retirement audit verdicts for Python-side scanner components and legacy
entry points. Each entry names its owner, its production consumers, the verdict,
and the falsifiable condition under which the verdict must be re-examined.

## 1. Verdict summary

| Component | Verdict | Re-examination point |
| :--- | :--- | :--- |
| Python scanner production arm (`struct_scan.py --result-json`, full-scan path) | **Retain** | R4.3 (Python exit stage) |
| Provider switch-back capability (rust→python) | **Retain** | R4.3, same batch as the production arm |
| Python hook fallback (`run_python_fallback` → hook scripts → `struct_scan.py`) | **Retain** | R4.3, same batch as switch-back |
| `--worker-config-json` plaintext-secret probe channel (G4) | **Retain** (bound to the Python worker arm lifecycle) | R4.3, same batch as switch-back |
| `index_mcp_queries.py` compatibility shell | **Retain** | First substantive release after v1.7.1 |
| `struct_scan.py` compatibility entry point | **Retain** | R4.3 (consumer set must be empty first) |
| Migration ladder 6→12 (Python owner) | **Retain, frozen** (R4.2 ruling: the Rust owner supports the current version only; the ladder is not replicated) | R4.3 (retired with the Python scanner) |
| state.db v1→v2 migration + legacy manifest translation layer | **Retain** | v2.0.0 release audit (H8-B2/B6) |
| `install.py` v2 dead arms (`write_manifest`/`do_install`/`do_uninstall`/`do_verify`) | **Delete** (this batch) | — |

## 2. Retained components: evidence and conditions

### 2.1 Python scanner production arm and switch-back capability

The deletion-candidate dependency chain, fixed as the audit's first evidence item:

```
oracle regeneration tooling ← Python scanner ← Python full_scan arm ← provider switch-back
```

- The stability window closed early on 2026-08-22 by user ruling; the calendar
  criterion (~1.9 of 7 days) is a registered residual risk. Switch-back is the
  only hedge against that residual risk. Deleting the Python worker arm removes
  the hedge while the risk is still open.
- `struct_scan.py --result-json` without `--files` is the machine contract the
  daemon uses for a rust→python switch-back full scan (R3.5b).
- Oracle regeneration (pinned venv) depends on the Python scanner permanently as
  a development-time owner; it is excluded from deletion accounting, not counted
  as a blocker.

**Re-examination criterion (R4.3 — amended by ruling A, 2026-08-28)**: the
original "one full release cycle" wording carried no operational definition,
and reading "the next release event" as the gate was circular (the next release
is v2.0.0, owned by R4.4, which postdates R4.3). Amended criterion: a seven-day
window — 2026-08-26 (R4.1 close) through 2026-09-02 — during which the rust
provider serves as the sole production provider with `diagnostic` empty. The
window gates **deletion commits only**; non-deletion work (pre-research, audit,
INV-R1 amendment, sample-source replacement) starts immediately. Operational
definition: deletion commits land only after 2026-09-02, and only after a
tail-of-window re-check of the diagnostic evidence (full `provider_sync` line
sweep of `daemon.log` plus `remy-cc daemon status --json`). Window-opening
evidence (2026-08-28 sweep): every `provider_sync` since the switch shows
`published=rust` / `diagnostic=null`. Retiring switch-back unlocks — but does
not itself decide — deletion of the Python worker arm and the G4 probe channel
(§2.4); those deletions are settled by the §8 audit record.

### 2.2 Python hook fallback and INV-R1

`hook_client.rs` (`run()`): the daemon IPC path falls back to
`run_python_fallback`, which spawns the deployed Python hook scripts, which in
turn subprocess `struct_scan.py`. Deleting the fallback means: daemon
unavailable ⇒ incremental indexing halts.

- INV-R1 (daemon-optional) remains a cross-stage hard invariant of the master
  plan. Narrowing it requires a master-plan amendment, not a retirement entry.
- Redundancy assessment (2026-08-23): the fallback is one arm with two consumer
  hooks reusing the same `struct_scan.py` entry the worker arm uses — no
  duplicate owner, no separate parity obligation. Its marginal maintenance cost
  is near zero while the switch-back arm and the oracle keep the Python scanner
  alive.

**Re-examination criterion (R4.3, "last production consumer" rule)**: when the
switch-back arm retires, the hook fallback becomes the last production consumer
of the Python scanner. Keeping an entire scanner alive for one exception path
is the over-redundancy threshold: at that point the fallback must be either
retired together with the scanner or explicitly re-justified against a rewritten
INV-R1.

**Settled (R4.3 audit, 2026-08-28 — §8)**: INV-R1 was amended first (pure
narrowing, parent plan §2; no journal or spawn-based Rust successor), and the
hook fallback retires together with the Python *production arm*. "Retired
together with the scanner" is read as the production arm, not as in-repo module
deletion: the deletion face is arms / flags / routes only. The scanner module
set (`scanner.py`, `parsers/`, `schema.py`, and the `struct_scan.py` entry)
stays alive whole as the development-time owner — `oracle/manifest.py` imports
the module set in-process via `sys.path`, and `oracle/bench.py` consumes the
human-output CLI arm.

### 2.3 Compatibility shells

- `index_mcp_queries.py` is a pure re-export shell (A1.2) and the live import
  path of `index_mcp_server.py:28`; eval (`arms.py`, `build_tasks.py`,
  `retrieval_baseline.py`) and two test modules consume it. The A1.2 promise is
  "at least one release period", ruled as: retirement is unlocked by the first
  substantive release after v1.7.1. Retirement order: change the
  `index_mcp_server.py:28` import to the owner modules first, then migrate
  eval/test consumers, then delete the shell.
- `struct_scan.py` remains the stable CLI entry consumed by the enrichment hook,
  the lifecycle hook, the daemon Python worker arm, and `run.py` (in-process
  import). Its retirement precondition is the emptying of that consumer set
  (R4.3 scope), not a release-period clock.

### 2.4 G4: plaintext-secret probe channel

`--worker-config-json` prints `secret_values` in clear text on stdout by
contract (worker/probe consumers: `worker.rs:261`, `provider.rs:258`). The
channel shares the Python worker arm's lifecycle: it is reviewed and retired in
the same R4.3 batch. Until then, the standing mitigation is operational (no
manual invocation in logged terminals).

**Settled (R4.3 audit, 2026-08-28 — §8)**: the channel retires whole with the
worker arm — the Rust probe consumer (`provider.rs::validate_python`) is
deleted in the daemon-side retirement commit, the `--worker-config-json` arm
and `_worker_config` in the Python-side deletion commit. No secret-free probe
variant is retained; deletion commits are gated by the §2.1 window.

### 2.5 Compatibility floor

- Minimum supported starting point: **v1.4.0** (logic index schema 6.0.0;
  lossless upgrades run through the frozen Python ladder until R4.3, and
  through backup-and-rebuild thereafter).
- Ladder ownership ruling (R4.2, 2026-08-25 — supersedes the earlier
  "move whole" form): the Rust owner supports the current schema version only.
  On open, a database below the current version — or one holding tables but no
  version row — is backed up to `.bak` (SQLite backup API) and rebuilt from the
  current schema; an incremental entry then escalates to the full file set
  within the same locked call. A database above the current version, or with an
  unparseable version string, is refused unchanged. The six ladder segments are
  not replicated in Rust. Data cost accepted by the ruling: `summary_versions`
  content is not carried over (it survives in `.bak`; regeneration uses the
  existing bootstrap pipeline). The public floor raise is declared at the
  v2.0.0 release audit (H8-B6); the parent plan's R4.2
  "minimum-compatible-version truncation ruling" slot is settled by this entry.
- The Python ladder (`migrations.py`) is frozen until R4.3: no segment edits;
  the single exception is a synchronized segment addition if a schema bump
  ships inside the window (the Rust rebuild floor tracks `SCHEMA_VERSION`
  automatically and needs no change).
- **Settled (R4.3 audit, 2026-08-28 — §8)**: the six ladder segments,
  `MIGRATION_HANDLERS`, `_resolve_migration_path`, and `migrate_json` are
  deleted in the Python-side deletion commit; `initialize_database` becomes
  fail-closed (any non-current version errors out with the database preserved
  unchanged), closing the lossless-upgrade channel as an intentional act
  confirmed by explicit refusal tests. The rebuild-test sample source moves
  from the ladder factories to frozen DDL snapshots
  (`tests/schema_snapshots.py`, v6/v7/v10), landed inside the window before
  any deletion; `ladder_samples.py` is not rewritten inside the window.
- daemon state schema v1→v2 migration and the installer's legacy manifest
  translation layer (`facade._parse_legacy_manifest`) remain until the v2.0.0
  release audit rules on the legacy-install population (H8-B2/B6).

## 3. Deleted components: evidence

### 3.1 install.py v2 dead arms (B3-1)

`main()` routes all three operations to the v3 facade (`do_install_v3` /
`do_uninstall_v3` / `do_verify_v3`); the v2 bodies (`write_manifest` L646,
`do_install` L1080, `do_uninstall` L1275, `do_verify` L1355, ≈500 lines) have no
caller. Static call-graph analysis over `install.py` confirms the v2-only helper
set is empty — every helper the v2 arms call is shared with v3 and is retained.
Uninstalling a legacy (v2-manifest) installation is served by the facade's
read-time translation layer, not by the v2 arms; deleting the arms does not
affect that capability. Tests that invoked the v2 arms directly are removed with
them; assertions that encode legacy-manifest semantics are covered by the
translation-layer tests (`test_cli_manifest.py`).

## 4. C2 settlement record

The cross-language symbol-hash ruling (C2) is settled in this batch: Python-side
docstrings are removed from the symbol hash input on both implementations
(parser-span based removal; `CACHE_CONTRACT_VERSION` 2→3), closing the
`python-docstring-in-hash` known gap in the oracle manifest. Ordering constraint
honored: F.1 (differential baseline change) landed first; C2 (oracle identity
change) second; the two never overlapped.

## 5. R4 handover items

| Item | Destination |
| :--- | :--- |
| Switch-back + hook fallback + G4 channel joint review | R4.3 — audited 2026-08-28 (§8); deletion commits gated to after 2026-09-02 |
| `struct_scan.py` consumer-set emptying (hooks, worker, run.py) | R4.3 — audited 2026-08-28 (§8); routes move to `remy-daemon scan` |
| `index_mcp_queries.py` shell retirement (import rewiring first) | first substantive release after v1.7.1 |
| Migration ladder ownership (current-version rebuild semantics; ladder not replicated) | R4.2 — settled 2026-08-25 (§2.5) |
| Legacy manifest translation window closure | v2.0.0 release audit (H8-B2/B6) |
| rconfig dual-owner registry single-sourcing after Python scanner exit | R4.3 |
| Probe corpus / parser support-matrix consistency check | standing (§9 matrix) |

## 6. Python exit boundary (H.6, R4.0 audit record)

Audit date: 2026-08-23 (R4.0). "Python exit" (R4.3) is defined as
**production-path exit**. Every Python-side component belongs to exactly one
class below; R4.3 deletion accounting, H8-B2 (settings merge host language) and
H8-D3 (diagnostics ownership) consume this table as the boundary authority.

| Class | Components | Verdict |
| :--- | :--- | :--- |
| 1. Production worker arm | Python scanner production arm (`struct_scan.py --result-json` full-scan path), daemon Python worker arm, rust→python switch-back, hook fallback, G4 probe channel | Exits at R4.3, gated by the §2.1/§2.2 criteria |
| 2. Hooks proper | `hooks/*.py` runtime hooks (R2 moved bookkeeping only, not the hook bodies) | Stays Python (non-exit) |
| 3. Config & CLI surface | `config_ui.py`, `remy_config.py` registry, `cli.py` — all subcommand families including `summary-*` — and the `remy-cc` shim | Stays Python (non-exit); the shim's post-I3 target is ruled at R4.4 (H8-B5) |
| 4. Development-time tools | `oracle/`, `eval/` | Permanent; never counted as exit blockers |

H.5 ruling (R4.0, 2026-08-23): the **summary runtime**
(`summarizer` / `propagation` / `llm_judge` / `bootstrap` + `llm_client`)
stays Python long-term and is accounted under class 3's lifecycle. The MCP
`query_navigate` LLM channel is re-implemented in Rust at R4.1 (reqwest,
OpenAI wire protocol, single POST; TLS key semantics ported from
`REMY_LLM_TLS_INSECURE`). The navigate prompt is an embedded string, so D2
(prompt asset root) is not touched by R4.1; it becomes relevant only if a
future ruling rewrites the summary runtime itself.

`remy_config.py` is alive under any ruling (G2, verified: 29 production
imports across hooks / skills / MCP / cli / install / config UI); the R4.3
rconfig single-sourcing item is therefore a contract-sync-ownership question,
not a survival question.

## 7. Python MCP server: deployment-face retirement (R4.1 audit record)

Audit date: 2026-08-26 (R4.1). The MCP read path moved into the Rust host
(`remy-daemon mcp`, rmcp 3.1.4, per-session stdio; INV-R2 topology unchanged).
Retirement is **deployment-face only**, consistent with the §6 boundary:

- `remy_mcp.json` now registers `~/.remy-cc/bin/remy-daemon` + `["mcp"]`
  (expanded to the absolute binary path by `install.py::register_mcp_server`).
- The six `index_mcp_*.py` entries left `DEPLOY_FILES_MAP`; already-deployed
  copies are removed by the next install transaction's delete semantics.
- The `mcp` SDK is no longer a required install dependency
  (`_prepare_dependencies` required-check dropped); it stays in
  `requirements-dev.txt` for the retained consumers.
- **Retained in-repo** (§6 class 4): `remy-src/index_mcp_server.py` and the
  five owner modules stay as the differential oracle and as live consumers for
  `eval/arms.py` (FastMCP `list_tools()` schema source) and the test modules
  (`test_freshness.py`, `test_mcp_server_invariants.py`,
  `test_retrieval_baseline.py`, `test_mcp_rust_parity.py`).
- Differential evidence: `tests/test_mcp_rust_parity.py` (H.4 matrix; 10 tools
  byte-level after warning-prefix strip, search/navigate ordered node_ref
  sequence). Re-examination point: if the Python oracle modules ever lose
  their last development-time consumer, they retire as a normal class-4
  cleanup, no new audit required.

## 8. R4.3 audit record (2026-08-28)

Audit date: 2026-08-28 (packet `task_20260828_020823`, anchor `a474e5f`).
Seventeen rulings locked across three `AskUserQuestion` rounds plus a scenario
confirmation gate. Commit sequence: 0 (INV-R1 amendment, parent plan) → 1 (this
record and the ruling texts) → 2 (sample source) land inside the window;
3a (Rust-side retirement) → 3b (consumer route changes) → 3c (Python-side
deletion) → 4 (doc-sync) land after 2026-09-02, each preceded by the §2.1
tail-of-window diagnostic re-check. 3a precedes 3c: only after the Rust side
stops routing to the fallback do the Python hook scripts become orphans.

| # | Ruling |
| :--- | :--- |
| 1 | Criterion amendment = ruling A: seven-day window through 2026-09-02, gating deletion commits only (§2.1) |
| 2 | INV-R1 disposition = pure narrowing; journal-based and spawn-based Rust successors both rejected. Degradation face is Dirty submission only (enrichment injection is a Rust direct read); staleness closes via struct_hash eventual consistency at the next scan; the parent-plan §5.1 race narrative collapses to single-writer. **Execution note (2026-08-28)**: the "Rust direct read" reading missed the freshness segment's IPC connect dependency; `7a884a5` degrades a failed connect to an empty freshness signal, so enrichment output survives daemon downtime |
| 3 | §2.2 deletion-face wording: arms / flags / routes only; the scanner module set stays alive as development-time owner (§2.2) |
| 4 | G4 retires whole with the worker arm; no secret-free probe variant (§2.4) |
| 5 | `struct_scan.py` CLI entry survives; retained surface = human-output arm, `--result-json`, `--files` / `--cwd` / `--lock-timeout` (oracle `bench.py` consumes the human arm) |
| 6 | Rebuild-test sample source = new `tests/schema_snapshots.py` frozen DDL snapshot factories (v6/v7/v10), built once from the live handlers and fixed via normalized `iterdump`, with a one-time equivalence verification inside the commit; `ladder_samples.py` must not be rewritten inside the window |
| 7 | Ladder deletion: six segments + `MIGRATION_HANDLERS` + `_resolve_migration_path` go; `initialize_database` becomes fail-closed with explicit refusal tests (§2.5) |
| 8 | `migrate_json` deleted; the TESTING R4.2 window-discipline entry is closed in the doc-sync commit as the intentional-closure record |
| 9 | `run.py` route (both arms): structure phase becomes `remy-daemon scan --result-json` via subprocess; the semantic phase opens its own connection (WAL, busy_timeout, `meta.version == 12.0.0` assertion) and does not create or replay schema; the lock window between the two phases admits other writers without correctness impact |
| 10 | `lifecycle_hook.run_struct_scan` spawns `remy-daemon scan` isomorphically: binary discovery `~/.remy-cc/bin/` plus dev-tree `target/{release,debug}` fallback, timeout = `REMY_STRUCT_SCAN_TIMEOUT` + `REMY_INDEX_SCAN_LOCK_TIMEOUT` + 5 s, `--lock-timeout` passthrough, missing binary → one stderr line and skip |
| 11 | Python dirty queue retires end to end (`DirtyQueue` / `DirtyClaim` / `manage_dirty` / `--consume-dirty` / both hook files); lifecycle adds a one-shot sweep of `.claude/logic_index_dirty*` residues |
| 12 | `REMY_SCANNER_PROVIDER` config key deleted; residual user values are zero-noise (Python: unregistered keys fall silently into the unknown bucket; Rust: the read chain is deleted) |
| 13 | `REMY_MIGRATION_KEEP_JSON` config key deleted with `migrate_json` |
| 14 | Install `hook_mode="python"` arm deleted; `facade._select_daemon` degradation branch becomes an error with guidance instead of a python fallback |
| 15 | `state.db` historical `provider='python'` rows: zero migration — provider is snapshotted by UPDATE at claim time and read paths carry no validation; startup sync overwrites published unconditionally with rust. The DDL literal `CHECK (provider IN ('python', 'rust'))` on the jobs and published rows is retained intentionally for historical-row tolerance (state schema v2 not bumped) |
| 16 | `python.json` runtime descriptor: daemon-side consumer chain deleted this batch; the install-side probe and deployment survive until R4.4 (install.py retirement) |
| 17 | Acceptance = three-channel reference sweep (grep symbol list + `query_callers` + importlib string scan, since `query_dependencies` cannot see dynamic imports), per-commit full pytest + pyright, cargo fmt/clippy/test after Rust commits, explicit oracle/eval/`.oracle-venv` zero-impact check, on-machine probe preceded by `cargo build --release`, dual-platform CI under user confirmation |
