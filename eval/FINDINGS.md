# M4 — First A/B Baseline: Findings

Archived conclusion for milestone M4 of the Remy-CC retrieval eval.

- run_id: `20260719_184701`
- model: `deepseek-v4-flash` · endpoint: `api.deepseek.com/v1/chat/completions`
- config: 12 tasks × {A-baseline, B-remy} × 3 reps, median-over-reps aggregation
- target: Remy-CC self (scoped `logic_index.db`); GT: pyright (non-circular)

Confidence levels below follow the project's 5-level scale (5 = verified by this
run's data, 4 = hypothesis, 2 = risk). All numbers are medians over 3 reps for
this single run and model; a second run flipped two task signs (see Variance),
so treat magnitudes as indicative, not settled.

## Headline (Level 5, this run)

| metric | direct (n=6) | multi_hop (n=6) | overall (n=12) |
| :-- | --: | --: | --: |
| ΔF1 (B−A) | +0.000 | +0.500 | +0.000 |
| Δtokens | −6 885 | −41 746 | −8 410 |
| Δcalls | −1.0 | −4.0 | −2.0 |
| answer-rate A / B | — | — | 69% / 89% |

## What holds up

1. **Cost: B-remy is cheaper on almost every task (Level 5).** 10 of 12 tasks
   show Δtokens < 0; the multi_hop median is −41 746 tokens. The two exceptions
   are t08 (+8 997) and t07 (+1 859), both small. On direct one-hop facts the
   token saving is real but modest (−6 885); grep can also answer these, just
   less cheaply — matching the plan's prediction that direct tier pays off on
   cost, not accuracy.

2. **Accuracy: B-remy wins the cross-file / two-hop tier (Level 5 for this run).**
   Clean wins where a call-graph index retrieves what line-oriented grep does
   not: t05 (+0.889), t10 (+1.000), t11 (+1.000), t12 (+1.000), t03 (+0.111).
   t10 is the sharpest: `query_symbol` + `query_callers` returned all 16 callers
   of `SymbolInfo` in 1–2 calls across 3/3 reps, while the baseline produced an
   empty answer in 3/3 reps at ~56 k tokens.

3. **Reliability: B-remy produces a usable answer more often (Level 5).**
   answer-rate 89% vs 69%. On the expensive cross-file tasks the baseline
   thrashes to an empty answer (t10 A = 0%, t12 A = 0%, t11 A = 33%); the tool
   arm converts those into answered, correct results.

## What does NOT hold up / must be corrected

4. **Refuted: "dataclass instantiation is a Remy blind spot" (was Level 4).**
   Both pyright (GT = 16 callers) and Remy's index record `SymbolInfo(...)`
   construction as caller edges. t10 is a Remy win, not a shared miss. The
   earlier README note claiming both arms miss it has been corrected.

5. **The two negative-ΔF1 tasks are answer-rate artifacts, not retrieval losses
   (Level 5).** t07 (−1.000) and t09 (−0.667): when B-remy emitted an answer it
   was correct (t07 rep0 = 1.000 via query_callers; t09 rep1 = 1.000), but 1–2
   of 3 reps ended without a fenced block → empty → F1 0, and the median of a
   bimodal `[1.0, 0.0, 0.0]` is 0.0. The `ans%` column surfaces this directly
   (t07 B = 33%). This is output-contract variance, not evidence the tool
   retrieves worse.

## Methodology corrections made this milestone

- **GT completeness (Level 5).** Hypothesis that pyright missed callers because
  it had not analyzed all files was refuted: opening all 67 project files before
  the query left 12/12 GTs identical. The one confirmed omission
  (`cmd_summary_rebuild` → `write_summary_version`) is a static-analysis limit on
  dynamic `importlib` imports, is symmetric across arms, and so does not bias
  ΔF1. Documented in `gen_gt.py`; no regeneration was needed.
- **Scorer salvage + format nudge.** `scorer.parse_answer` now recovers prose /
  numbered-list answers when the fenced block is dropped; `agent_loop` nudges
  once for the contract. These lifted answer-rate (A 67→69%, B 86→89% vs the
  prior run) but did not eliminate empties on the hardest tasks.
- **answer-rate reported separately from F1**, so contract compliance is no
  longer conflated with retrieval accuracy.

## Variance caveat (Level 2 risk)

At N=3 with a bimodal small model, per-task medians are unstable: between the
05:09 and 18:47 runs, t07 and t09 flipped sign purely on empty-answer counts.
Firmer conclusions need more reps (≥5) or a steadier model, and/or scoring that
reports the answered-only F1 alongside the median.

## Bottom line

The harness is operational and the A/B signal is legible: on this run and model,
Remy-CC's tools cut token cost across the board and materially raise accuracy on
cross-file / two-hop caller questions, while making no difference on one-hop
definition lookups (as expected). The residual noise is output-contract
variance, now measured (answer-rate) rather than hidden.
