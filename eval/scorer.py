"""
Scorer for code-retrieval tasks (set recall / precision / F1).

The set recall/precision/F1 arithmetic is vendored from KBench
`kbench/scorers/set_recall.py` (github.com/ajksunkang-aios/KBench, MIT License)
to keep the eval subsystem self-contained with no runtime dependency on a
KBench checkout.

ground_truth shape:
  {"method": "set", "expected": [["name", "path"], ...]}
"""
from __future__ import annotations

import re

from .paths import norm_path, norm_name

_FENCE_RE = re.compile(r"```kbench\s*\n(.*?)```", re.DOTALL)
_PIPE_RE = re.compile(r"([A-Za-z_][\w.]*(?:::[\w.]+)?)\s*\|\s*([^\s|`]+)")
_PY_PATH_RE = re.compile(r"[\w./\\-]+\.py")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*(?:::[\w.]+)?")


def _add_block_line(line: str, items: set, repo_prefix: str | None) -> None:
    """
    Parse one fenced-block line in the contract format `name|file`.
    """
    line = line.strip().strip("`").strip()
    if not line or line.startswith("kbench"):
        return
    parts = line.split("|", 1)
    name = norm_name(parts[0])
    if not name:
        return
    path = norm_path(parts[1], repo_prefix) if len(parts) > 1 else ""
    items.add((name, path))


def _salvage_line(line: str, items: set, repo_prefix: str | None) -> None:
    """
    Best-effort extraction from a prose line when no fenced block was emitted.
    """
    pm = _PIPE_RE.search(line)
    if pm:
        name = norm_name(pm.group(1))
        if name:
            items.add((name, norm_path(pm.group(2), repo_prefix)))
        return
    paths = _PY_PATH_RE.findall(line)
    if len(paths) != 1:
        return
    names = [t for t in _BACKTICK_RE.findall(line)
             if "/" not in t and "\\" not in t and not t.endswith(".py")
             and _IDENT_RE.fullmatch(t)]
    if not names:
        return
    items.add((norm_name(names[0]), norm_path(paths[0], repo_prefix)))


def parse_answer(answer: str, repo_prefix: str | None = None) -> set[tuple[str, str]]:
    """
    Return the set of (name, normalized_path) items the agent asserted.
    """
    items: set[tuple[str, str]] = set()
    text = answer or ""
    m = _FENCE_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            _add_block_line(line, items, repo_prefix)
        return items
    for line in text.splitlines():
        _salvage_line(line, items, repo_prefix)
    return items


def _gt_items(expected: list, repo_prefix: str | None = None) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for e in expected:
        if isinstance(e, (list, tuple)):
            name = norm_name(str(e[0]))
            path = norm_path(str(e[1]), repo_prefix) if len(e) > 1 else ""
        else:
            name, path = norm_name(str(e)), ""
        out.add((name, path))
    return out


def score(answer: str, ground_truth: dict, repo_prefix: str | None = None) -> dict:
    """
    Return {recall, precision, f1, matched, predicted_n, expected_n}.
    """
    expected = _gt_items(ground_truth.get("expected", []), repo_prefix)
    predicted = parse_answer(answer, repo_prefix)

    if not predicted and not expected:
        return {"recall": 1.0, "precision": 1.0, "f1": 1.0,
                "matched": 0, "predicted_n": 0, "expected_n": 0}

    matched = predicted & expected
    recall = len(matched) / len(expected) if expected else 0.0
    precision = len(matched) / len(predicted) if predicted else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"recall": round(recall, 4), "precision": round(precision, 4),
            "f1": round(f1, 4), "matched": len(matched),
            "predicted_n": len(predicted), "expected_n": len(expected)}
