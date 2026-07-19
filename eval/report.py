"""
Aggregate eval records into an A/B comparison report.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

_METRICS = ("f1", "recall", "precision", "tokens_in", "tokens_out",
            "tokens_total", "calls", "turns", "wall")


def _calls_total(rec: dict) -> int:
    return sum((rec.get("tool_calls") or {}).values())


def aggregate(records: list) -> dict:
    """
    Median every metric over reps for each (task, arm).
    """
    grouped = defaultdict(list)
    tiers: dict = {}
    tasks: list = []
    arms: list = []
    for r in records:
        grouped[(r["task"], r["arm"])].append(r)
        tiers.setdefault(r["task"], r.get("tier", "?"))
        if r["task"] not in tasks:
            tasks.append(r["task"])
        if r["arm"] not in arms:
            arms.append(r["arm"])

    cell: dict = {}
    for key, recs in grouped.items():
        series = {
            "f1": [x["f1"] for x in recs],
            "recall": [x["recall"] for x in recs],
            "precision": [x["precision"] for x in recs],
            "tokens_in": [x["tokens_in"] for x in recs],
            "tokens_out": [x["tokens_out"] for x in recs],
            "tokens_total": [x["tokens_in"] + x["tokens_out"] for x in recs],
            "calls": [_calls_total(x) for x in recs],
            "turns": [x["turns"] for x in recs],
            "wall": [x["wall"] for x in recs],
        }
        c = {m: statistics.median(v) for m, v in series.items()}
        c["n_reps"] = len(recs)
        c["answer_rate"] = sum(1 for x in recs if x["predicted_n"] > 0) / len(recs)
        cell[key] = c
    return {"cell": cell, "tasks": tasks, "arms": arms, "tiers": tiers}


def gains(agg: dict, baseline: str, treatment: str) -> dict:
    """
    Median (treatment - baseline) deltas, overall and per tier.
    """
    cell, tasks, tiers = agg["cell"], agg["tasks"], agg["tiers"]
    per_tier_deltas = defaultdict(list)
    overall: list = []
    for t in tasks:
        b = cell.get((t, baseline))
        r = cell.get((t, treatment))
        if not b or not r:
            continue
        delta = {m: r[m] - b[m] for m in _METRICS}
        per_tier_deltas[tiers[t]].append(delta)
        overall.append(delta)

    def _median_delta(deltas: list) -> dict:
        out = {}
        for m in _METRICS:
            xs = [d[m] for d in deltas]
            out[m] = statistics.median(xs) if xs else None
        out["n"] = len(deltas)
        return out

    per_tier = {tier: _median_delta(ds) for tier, ds in per_tier_deltas.items()}
    return {"per_tier": per_tier, "overall": _median_delta(overall)}


def _f1(v) -> str:
    return "-" if v is None else f"{v:+.3f}"


def _n1(v) -> str:
    return "-" if v is None else f"{v:+.1f}"


def _i0(v) -> str:
    return "-" if v is None else f"{v:+.0f}"


def _answer_rates(agg: dict) -> dict:
    """
    Mean answer-rate per arm across tasks (fraction of reps with a usable answer).
    """
    cell, tasks, arms = agg["cell"], agg["tasks"], agg["arms"]
    out = {}
    for a in arms:
        rates = [cell[(t, a)]["answer_rate"] for t in tasks if (t, a) in cell]
        out[a] = statistics.mean(rates) if rates else 0.0
    return out


def _model(records: list):
    agg = aggregate(records)
    arms = agg["arms"]
    baseline = arms[0] if arms else None
    treatment = arms[1] if len(arms) > 1 else None
    g = gains(agg, baseline, treatment) if treatment else None
    return agg, baseline, treatment, g


def render_terminal(records: list, meta: dict | None = None) -> str:
    agg, baseline, treatment, g = _model(records)
    cell, tasks, tiers, arms = agg["cell"], agg["tasks"], agg["tiers"], agg["arms"]
    L: list = []
    if meta:
        L.append(f"model={meta.get('model')}  reps={meta.get('reps')}  "
                 f"arms={arms}  target={meta.get('target')}")
    if g:
        o = g["overall"]
        L.append("")
        L.append(f"OVERALL ({treatment} vs {baseline}, n={o['n']}): "
                 f"ΔF1={_f1(o['f1'])}  Δtokens={_i0(o['tokens_total'])}  "
                 f"Δcalls={_n1(o['calls'])}  Δturns={_n1(o['turns'])}")
        ar = _answer_rates(agg)
        L.append("answer-rate: " + "  ".join(f"{a}={ar[a]*100:.0f}%" for a in arms))
        L.append("")
        L.append(f"  {'tier':<12} {'n':>2}  {'ΔF1':>7}  {'Δtokens':>9}  "
                 f"{'Δcalls':>7}  {'Δturns':>7}")
        for tier, gd in sorted(g["per_tier"].items()):
            L.append(f"  {tier:<12} {gd['n']:>2}  {_f1(gd['f1']):>7}  "
                     f"{_i0(gd['tokens_total']):>9}  {_n1(gd['calls']):>7}  "
                     f"{_n1(gd['turns']):>7}")
    L.append("")
    L.append("per-task (median over reps):")
    header = f"  {'task':<34} {'tier':<10}"
    for a in arms:
        header += f" {a + ' f1':>13} {a + ' tok':>13}"
    if treatment:
        header += f" {'ΔF1':>7} {'Δtok':>8}"
    L.append(header)
    for t in tasks:
        row = f"  {t:<34} {tiers[t]:<10}"
        for a in arms:
            c = cell.get((t, a))
            row += (f" {c['f1']:>13.3f} {c['tokens_total']:>13.0f}"
                    if c else f" {'-':>13} {'-':>13}")
        if treatment:
            b, r = cell.get((t, baseline)), cell.get((t, treatment))
            if b and r:
                row += (f" {r['f1'] - b['f1']:>+7.3f} "
                        f"{r['tokens_total'] - b['tokens_total']:>+8.0f}")
            else:
                row += f" {'-':>7} {'-':>8}"
        L.append(row)
    return "\n".join(L)


def render_markdown(records: list, meta: dict | None = None) -> str:
    agg, baseline, treatment, g = _model(records)
    cell, tasks, tiers, arms = agg["cell"], agg["tasks"], agg["tiers"], agg["arms"]
    M: list = ["# Remy-CC A/B Eval Report", ""]
    if meta:
        M.append(f"- model: `{meta.get('model')}`")
        M.append(f"- reps: {meta.get('reps')}")
        M.append(f"- arms: {', '.join(arms)}")
        M.append(f"- target: `{meta.get('target')}`")
        M.append(f"- endpoint: `{meta.get('base_url')}`")
        if meta.get("run_id"):
            M.append(f"- run_id: `{meta['run_id']}`")
        M.append("")
    if g:
        o = g["overall"]
        M.append(f"## Overall gain ({treatment} − {baseline})")
        M.append("")
        M.append(f"n={o['n']} tasks. ΔF1={_f1(o['f1'])}, "
                 f"Δtokens={_i0(o['tokens_total'])}, "
                 f"Δcalls={_n1(o['calls'])}, Δturns={_n1(o['turns'])}.")
        M.append("")
        ar = _answer_rates(agg)
        M.append("Answer-rate (reps with a usable answer): "
                 + ", ".join(f"{a} {ar[a]*100:.0f}%" for a in arms) + ".")
        M.append("")
        M.append("## Gain by tier")
        M.append("")
        M.append("| tier | n | ΔF1 | Δtokens | Δcalls | Δturns |")
        M.append("| :-- | --: | --: | --: | --: | --: |")
        for tier, gd in sorted(g["per_tier"].items()):
            M.append(f"| {tier} | {gd['n']} | {_f1(gd['f1'])} | "
                     f"{_i0(gd['tokens_total'])} | {_n1(gd['calls'])} | "
                     f"{_n1(gd['turns'])} |")
        M.append("")
    M.append("## Per-task (median over reps)")
    M.append("")
    head = "| task | tier |"
    sep = "| :-- | :-- |"
    for a in arms:
        head += f" {a} F1 | {a} tokens | {a} ans% |"
        sep += " --: | --: | --: |"
    if treatment:
        head += " ΔF1 | Δtokens |"
        sep += " --: | --: |"
    M.append(head)
    M.append(sep)
    for t in tasks:
        row = f"| {t} | {tiers[t]} |"
        for a in arms:
            c = cell.get((t, a))
            row += (f" {c['f1']:.3f} | {c['tokens_total']:.0f} | {c['answer_rate']*100:.0f} |"
                    if c else " - | - | - |")
        if treatment:
            b, r = cell.get((t, baseline)), cell.get((t, treatment))
            if b and r:
                row += (f" {r['f1'] - b['f1']:+.3f} | "
                        f"{r['tokens_total'] - b['tokens_total']:+.0f} |")
            else:
                row += " - | - |"
        M.append(row)
    M.append("")
    return "\n".join(M)
