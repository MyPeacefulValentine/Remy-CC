"""
Task x arm x rep matrix runner.
"""
from __future__ import annotations

import json
from pathlib import Path

from .agent_loop import run_agent
from .arms import build_arm
from .scorer import score


def load_tasks(tasks_dir: Path, only: list[str] | None = None) -> list[dict]:
    tasks: list[dict] = []
    for p in sorted(Path(tasks_dir).glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8"))
        t.setdefault("id", p.stem)
        if only and t["id"] not in only:
            continue
        tasks.append(t)
    return tasks


def run_matrix(tasks: list[dict], arms: list[str], *, root, db_path,
               endpoint: dict, reps: int = 1,
               agent_kwargs: dict | None = None) -> list[dict]:
    root = Path(root).resolve()
    repo_prefix = root.name
    agent_kwargs = agent_kwargs or {}
    records: list[dict] = []
    for arm in arms:
        defs, dispatch = build_arm(arm, root, db_path)
        for task in tasks:
            for rep in range(reps):
                m = run_agent(task["prompt"], defs, dispatch,
                              **endpoint, **agent_kwargs)
                sc = score(m["answer"], task["ground_truth"], repo_prefix=repo_prefix)
                records.append({
                    "task": task["id"],
                    "tier": task.get("tier", "?"),
                    "arm": arm,
                    "rep": rep,
                    "f1": sc["f1"],
                    "recall": sc["recall"],
                    "precision": sc["precision"],
                    "matched": sc["matched"],
                    "predicted_n": sc["predicted_n"],
                    "expected_n": sc["expected_n"],
                    "tokens_in": m["tokens_in"],
                    "tokens_out": m["tokens_out"],
                    "tokens_cache": m["tokens_cache"],
                    "tool_calls": m["tool_calls"],
                    "turns": m["turns"],
                    "wall": m["wall_seconds"],
                    "answer": m["answer"],
                    "tool_trace": m["tool_trace"],
                })
    return records
