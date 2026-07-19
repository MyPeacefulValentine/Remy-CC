"""Remy-CC eval — A/B retrieval benchmark for the Remy-CC MCP tools.

Measures whether attaching Remy-CC's MCP code-intelligence tools makes a
tool-using agent more accurate (F1) and cheaper (tokens / tool-calls) than a
grep/glob/read baseline, over a task set with independently-generated
(non-circular) ground truth.

Methodology follows KBench (github.com/ajksunkang-aios/KBench): same agent,
same task set, the only variable is whether the system-under-test is attached.
Arms: A-baseline (grep/glob/read) vs B-remy (baseline + Remy-CC MCP tools).
"""
