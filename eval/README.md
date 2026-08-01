# Remy-CC Retrieval Evaluation

This package contains two retrieval evaluations:

1. an agent A/B harness that compares baseline source tools with Remy-CC MCP
   tools; and
2. a deterministic candidate-level baseline for the current FTS, LIKE, fuzzy,
   and public fallback pipeline.

## Agent A/B evaluation

The A/B harness measures whether an agent answers code-structure questions more
accurately or with fewer tokens, calls, and turns when Remy-CC's MCP tools are
attached.

## Arms

| arm | tools |
| :-- | :-- |
| `A-baseline` | `grep` / `glob` / `read`, pure-Python and cross-platform (no system `grep`), sandboxed under the target root |
| `B-remy` | baseline tools **plus** Remy-CC's MCP tools (`query_symbol`, `query_callers`, `query_callees`, `query_impact`, `query_flow`, …) |

`B-remy` loads the MCP tool schemas via FastMCP `list_tools()` and dispatches to
the same `@mcp.tool` wrappers the live server uses, so measured token cost equals
real usage. `query_navigate` is excluded because its LLM callback would fold LLM
cost into the retrieval-tool measurement.

## Ground truth is non-circular

GT is produced by **pyright** (`gen_gt.py`, over the language server's call
hierarchy + definitions) — a third-party semantic engine independent of
Remy-CC's own index. The system under test never grades itself. The target code
being Remy-CC's own source does not create circularity: code ≠ GT source.

**Known GT scope limit.** GT is pyright's *static* call graph. A caller that
reaches the target only through a dynamically imported module (e.g.
`importlib.import_module("summarizer")` in `remy-src/cli.py`, then
`summarizer.write_summary_version(...)`) is not statically resolvable and is
absent from GT by design. Such omissions are **symmetric across the two arms**,
so they do not bias `ΔF1` (the A/B comparison); they only slightly understate
both arms' absolute F1. Text-based GT augmentation is rejected: it would
reintroduce the baseline arm's own retrieval method into the ground truth.

## Layout

```
eval/
  cli.py          entry: load tasks → run arm×rep matrix → render report
  runner.py       task×arm×rep matrix → per-episode records
  agent_loop.py   OpenAI native function-calling loop + metric instrumentation
  arms.py         BaselineTools (grep/glob/read) + RemyTools (MCP dispatch)
  scorer.py       set recall/precision/F1 on (name, file) pairs; vendored from KBench
  paths.py        path/name normalization shared by GT and scorer
  gen_gt.py       pyright LSP client → non-circular GT
  build_tasks.py  spec table → tasks/python/*.json via gen_gt
  report.py       median-over-reps aggregate, gain-by-tier, per-task, answer-rate
  retrieval_baseline.py  deterministic fixture, channel capture, ranking, timing
  tasks/python/   agent task set (*.json, KBench schema)
  tasks/retrieval_baseline/p1_1.json  declarative candidate-level ground truth
  baselines/p1_1.json  committed P1.1 reference snapshot
  .scoped/        scoped logic_index.db for B-remy (gitignored)
  results/ reports/  run artifacts (gitignored)
```

## Task tiers

- **direct** — one-hop, single-definition relations. The payoff Remy targets
  here is cost (fewer tokens/turns), not necessarily accuracy.
- **multi_hop** — same-name disambiguation, cross-file callers, two-hop chains
  where line-oriented grep over-matches or must be walked by hand. This is where
  a call-graph index can move `ΔF1`.

## Metrics

Scoring compares the `(name, normalized_file)` set the agent asserts against GT.
Names are reduced to their trailing segment (`Class.method` → `method`) so a
retrieval win is not lost to a naming-convention difference; paths are
normalized for separators and an accidental repo-name prefix.

The agent's final answer must be a fenced ` ```kbench ` block (`name|file` per
line). An empty block means "found nothing". If the model answers in prose
instead, `scorer.parse_answer` runs a conservative salvage pass; `agent_loop`
also nudges once for the contract format before accepting a block-less message.

**answer-rate** is reported separately from F1: the fraction of reps that
produced a parseable, non-empty answer. It isolates output-contract compliance
(did the model emit a usable answer at all) from retrieval accuracy (F1 given an
answer), so a run where a small model occasionally drops the block is not
silently read as a retrieval failure. Aggregation is median-over-reps per
(task, arm); use `--reps ≥ 3`.

## Usage

Run from the Remy-CC repo root (or set `PYTHONPATH` to it). The endpoint is read
from `OPENAI_BASE_URL` / `OPENAI_API_KEY` — the same variables remy-index uses.
`B-remy` requires a scoped `logic_index.db` via `--db`.

```bash
# smoke test: single task, single rep
python -m eval.cli --quick --db eval/.scoped/logic_index.db

# full baseline: 3 reps, both arms, save records + markdown report
python -m eval.cli --reps 3 --arms A-baseline B-remy --db eval/.scoped/logic_index.db --save

# regenerate the ground truth (pyright must be installed: pip install pyright)
python eval/build_tasks.py --root Remy-CC

# one GT entry, ad hoc
python eval/gen_gt.py --root Remy-CC --file skills/remy-index/summarizer.py \
    --symbol write_summary_version --kind callers
```

Reports land in `eval/reports/<run_id>/report.md`; raw per-episode records
(including full `tool_trace`) in `eval/results/<run_id>/records.json`.

## Deterministic candidate baseline

The `retrieval-baseline` compatibility subcommand does not use an API endpoint,
model, network, or parser. It creates a temporary schema 10.0.0 SQLite database
from `tasks/retrieval_baseline/p1_1.json`. The same file declares relevant node
identities and expected fallback channels. Expected values are reviewed data;
they are never generated from the functions under test.

Each task records the candidates and ranks returned independently by `_search_fts`,
`_search_like`, and `_search_fuzzy`, plus the formatted output from
`query_search_impl`. Metrics are Recall@1/5/10, MRR, actual no-result rate, and
expected-empty accuracy. Empty-ground-truth tasks are excluded from Recall and
MRR. Each task and channel is warmed up three times and measured thirty times
with `perf_counter_ns`; raw samples and nearest-rank P50/P95 values are retained.
No latency value is a pass threshold.

```bash
# create a timestamped raw result under eval/results/ (gitignored)
python -m eval.cli retrieval-baseline \
  --tasks eval/tasks/retrieval_baseline/p1_1.json \
  --navigate-db ../.claude/logic_index.db \
  --save

# explicitly replace the committed P1.1 reference snapshot after review
python -m eval.cli retrieval-baseline \
  --tasks eval/tasks/retrieval_baseline/p1_1.json \
  --navigate-db ../.claude/logic_index.db \
  --update-snapshot eval/baselines/p1_1.json
```

The record includes the Git commit, suite and schema versions, Python and
platform versions, parser configuration, database/WAL sizes, all timing samples,
and `query_navigate` corpus counts and prompt characters. Navigation measurement
only calls corpus and prompt helpers; it does not call an LLM or write the cache.
Normal runs cannot update the committed snapshot.

## Reading the results

- **direct tier** — expect `ΔF1 ≈ 0` with `Δtokens < 0`: the tool wins on cost,
  not accuracy, because grep can also find one-hop facts (just less cheaply).
- **multi_hop tier** — watch `ΔF1`: cross-file and two-hop caller questions are
  where a call-graph index should retrieve what line-oriented grep misses.

**When ΔF1 goes negative, check answer-rate first.** A small model is bimodal:
on the hardest cross-file tasks it sometimes ends a turn without the fenced
block, which scores as an empty (F1 = 0) answer even though the retrieval it did
was correct. At `--reps 3` the median of `[1.0, 0.0, 0.0]` is `0.0`, so two
dropped blocks can flip a task's sign. The `ans%` column separates this from a
real retrieval loss: a task where an arm's F1 fell but its `ans%` also fell is an
output-contract artifact, not evidence the arm retrieves worse. Raising reps or
using a steadier model tightens this; the nudge in `agent_loop` reduces but does
not eliminate it.

## Provenance

`scorer.py` vendors the set recall/precision/F1 arithmetic from KBench
(`kbench/scorers/set_recall.py`, MIT) with one deliberate divergence: the
comparison key is `(name, file)`, not name alone, so echoing back the
prompt-supplied symbol name does not score 1.0 without any retrieval.
