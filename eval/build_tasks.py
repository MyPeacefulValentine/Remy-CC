"""
Build the Python retrieval task set with pyright-backed non-circular ground
truth.

Each spec (id, tier, kind, file, symbol) is turned into a task JSON under
tasks/python/ by running gen_gt.generate (pyright call hierarchy). GT is
produced by pyright alone and never touches Remy-CC's own index, so scoring
does not use the system-under-test to grade itself.

Tiers:
  direct    — one-hop, single-definition relations; the payoff Remy targets
              here is cost (fewer tokens/turns), not necessarily accuracy.
  multi_hop — same-name disambiguation, cross-file callers, and two-hop chains
              where line-oriented grep over-matches or must be walked by hand;
              this is where a call-graph index can move ΔF1.

Prompts are tool-neutral (they never name a tool) and, for same-name symbols,
state the target's defining file so both arms are told which symbol is meant.

Run from the Remy-CC repo root:
    python Remy-CC/eval/build_tasks.py --root Remy-CC [--wait 12] [--only t07 ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_gt import generate

_OUT = Path(__file__).resolve().parent / "tasks" / "python"

# (id, tier, kind, file, symbol)
SPECS = [
    ("t01_def_write_summary_version", "direct", "def",
     "skills/remy-index/summarizer.py", "write_summary_version"),
    ("t02_def_with_freshness", "direct", "def",
     "remy-src/index_mcp_server.py", "_with_freshness"),
    ("t03_callers_with_freshness", "direct", "callers",
     "remy-src/index_mcp_server.py", "_with_freshness"),
    ("t04_callers_get_latest_summary", "direct", "callers",
     "remy-src/index_mcp_common.py", "get_latest_summary"),
    ("t05_callers_write_summary_version", "direct", "callers",
     "skills/remy-index/summarizer.py", "write_summary_version"),
    ("t06_callees_write_summary_version", "direct", "callees",
     "skills/remy-index/summarizer.py", "write_summary_version"),
    ("t07_disambig_line_number_at", "multi_hop", "callers",
     "skills/remy-index/parsers/c_cpp_parser.py", "_line_number_at"),
    ("t08_disambig_env_int", "multi_hop", "callers",
     "skills/remy-index/struct_scan.py", "_env_int"),
    ("t09_disambig_render_template", "multi_hop", "callers",
     "skills/remy-insight/render.py", "render_template"),
    ("t10_crossfile_symbolinfo", "multi_hop", "callers",
     "skills/remy-index/parsers/base.py", "SymbolInfo"),
    ("t11_twohop_get_latest_summary", "multi_hop", "callers2",
     "remy-src/index_mcp_common.py", "get_latest_summary"),
    ("t12_twohop_write_summary_version", "multi_hop", "callers2",
     "skills/remy-index/summarizer.py", "write_summary_version"),
]


def build_prompt(kind: str, symbol: str, file: str) -> str:
    if kind == "def":
        return (f"In the Remy-CC repository, find where the symbol named `{symbol}` "
                f"is defined. Report its name and the repo-relative path of the file "
                f"that defines it.")
    if kind == "callers":
        return (f"In the Remy-CC repository, find every function or method that "
                f"directly calls `{symbol}` (the one defined in `{file}`). Report "
                f"each caller's name and the repo-relative path of the file where "
                f"that caller is defined.")
    if kind == "callees":
        return (f"In the Remy-CC repository, find every project-defined function that "
                f"`{symbol}` (defined in `{file}`) directly calls. Report each "
                f"callee's name and the repo-relative path of the file where that "
                f"callee is defined.")
    if kind == "callers2":
        return (f"In the Remy-CC repository, find every function that indirectly calls "
                f"`{symbol}` (defined in `{file}`) through exactly one intermediate "
                f"function — that is, the direct callers of the functions that "
                f"directly call `{symbol}`. Report each such function's name and the "
                f"repo-relative path of the file where it is defined.")
    raise ValueError(f"unknown kind: {kind}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build pyright-backed task set")
    ap.add_argument("--root", default="Remy-CC",
                    help="target repo root (default: Remy-CC)")
    ap.add_argument("--wait", type=float, default=12.0,
                    help="seconds to let pyright analyze per task")
    ap.add_argument("--only", nargs="+", help="build only these task ids")
    args = ap.parse_args(argv)

    _OUT.mkdir(parents=True, exist_ok=True)
    root = Path(args.root)
    written, empties = 0, []
    for tid, tier, kind, file, symbol in SPECS:
        if args.only and tid not in args.only:
            continue
        gt = generate(root, file, symbol, kind, args.wait)
        task = {
            "id": tid, "tier": tier, "kind": kind,
            "symbol": symbol, "file": file,
            "prompt": build_prompt(kind, symbol, file),
            "ground_truth": gt,
        }
        (_OUT / f"{tid}.json").write_text(
            json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
        n = len(gt["expected"])
        written += 1
        if n == 0:
            empties.append(tid)
        print(f"  {tid:38} {kind:9} gt_items={n}")

    print(f"\nwrote {written} tasks to {_OUT}")
    if empties:
        print(f"WARNING empty GT (pyright found nothing): {empties}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
