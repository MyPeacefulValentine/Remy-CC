"""
Remy-CC eval CLI: load a task set, run the A/B matrix over an OpenAI-compatible
endpoint, print a per-episode summary, and optionally persist raw records.

Run from the Remy-CC repo root:
    python -m eval.cli --db .claude/logic_index.db --quick
    python -m eval.cli --reps 3 --arms A-baseline B-remy --db <scoped.db>
    python -m eval.cli retrieval-baseline --save

The endpoint is read from REMY_LLM_BASE_URL / REMY_LLM_API_KEY, shared with
remy-index. B-remy requires a scoped logic_index.db via --db.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .runner import load_tasks, run_matrix
from .report import render_terminal, render_markdown

_REMY_SRC = Path(__file__).resolve().parent.parent / "remy-src"
if str(_REMY_SRC) not in sys.path:
    sys.path.insert(0, str(_REMY_SRC))
import remy_config

_EVAL_DIR = Path(__file__).resolve().parent
_REMY_ROOT = _EVAL_DIR.parent
_DEFAULT_TASKS = _EVAL_DIR / "tasks" / "python"


def _endpoint(model: str) -> dict:
    config = remy_config.load_config(strict=True)
    base = config.get("REMY_LLM_BASE_URL")
    if not base:
        raise SystemExit("REMY_LLM_BASE_URL is not set (endpoint for the agent loop).")
    return {"base_url": base,
            "api_key": config.get("REMY_LLM_API_KEY", ""),
            "model": model}


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("remy-eval", description=__doc__)
    ap.add_argument("--arms", nargs="+", default=["A-baseline", "B-remy"],
                    help="arms to run (A-baseline / B-remy)")
    ap.add_argument("--reps", type=int, default=1, help="repetitions per task x arm")
    ap.add_argument("--model", default=remy_config.load_config(strict=True).get("REMY_EVAL_MODEL"))
    ap.add_argument("--target", type=Path, default=_REMY_ROOT,
                    help="source-tree root the baseline greps and B-remy queries (default: Remy-CC)")
    ap.add_argument("--db", type=Path, default=None,
                    help="scoped logic_index.db for B-remy")
    ap.add_argument("--tasks", type=Path, default=_DEFAULT_TASKS)
    ap.add_argument("--only", nargs="+", default=None, help="run only these task ids")
    ap.add_argument("--quick", action="store_true",
                    help="smoke test: first task, 1 rep")
    ap.add_argument("--max-turns", type=int, default=12)
    ap.add_argument("--save", action="store_true", help="write raw records to results/<run_id>/")
    return ap


def main(argv=None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv[:1] == ["retrieval-baseline"]:
        from .retrieval_baseline import main as retrieval_baseline_main
        return retrieval_baseline_main(effective_argv[1:])

    args = _build_parser().parse_args(effective_argv)

    tasks = load_tasks(args.tasks, args.only)
    if args.quick:
        tasks = tasks[:1]
        args.reps = 1
    if not tasks:
        raise SystemExit(f"no tasks found in {args.tasks}")

    if "B-remy" in args.arms and not args.db:
        raise SystemExit("B-remy requires --db <scoped logic_index.db>")
    if args.db and not Path(args.db).exists():
        raise SystemExit(f"--db not found: {args.db}")

    endpoint = _endpoint(args.model)
    records = run_matrix(
        tasks, args.arms, root=args.target, db_path=args.db,
        endpoint=endpoint, reps=args.reps,
        agent_kwargs={"max_turns": args.max_turns},
    )

    meta = {"model": args.model, "arms": args.arms, "reps": args.reps,
            "target": str(Path(args.target).resolve()),
            "db": str(args.db) if args.db else None,
            "base_url": endpoint["base_url"]}
    print(render_terminal(records, meta))

    if args.save:
        run_id = time.strftime("%Y%m%d_%H%M%S")
        meta["run_id"] = run_id
        out = _EVAL_DIR / "results" / run_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "records.json").write_text(
            json.dumps({"meta": meta, "records": records}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        rep_dir = _EVAL_DIR / "reports" / run_id
        rep_dir.mkdir(parents=True, exist_ok=True)
        (rep_dir / "report.md").write_text(
            render_markdown(records, meta), encoding="utf-8")
        print(f"saved: {out / 'records.json'}")
        print(f"report: {rep_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
