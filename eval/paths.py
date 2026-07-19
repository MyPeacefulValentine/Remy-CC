"""
Path normalization shared by the GT generator (gen_gt.py) and the scorer
(scorer.py), so ground truth and agent answers are compared on identical keys.
"""
from __future__ import annotations


def norm_path(path: str, repo_prefix: str | None = None) -> str:
    """
    Return a canonical repo-relative posix path, lowercased.
    """
    p = (path or "").strip().replace("\\", "/").lower()
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if repo_prefix:
        pref = repo_prefix.strip("/").lower() + "/"
        if p.startswith(pref):
            p = p[len(pref):]
    return p


def norm_name(name: str) -> str:
    """
    Return a canonical bare symbol name for set comparison.
    """
    s = (name or "").strip().strip("`*").strip()
    if "(" in s:                        # drop call/argument suffix: foo(x) -> foo
        s = s.split("(", 1)[0].strip()
    s = s.replace("::", ".").rsplit(".", 1)[-1]   # A.b / ns::b -> b
    return s.strip()
