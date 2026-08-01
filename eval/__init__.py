"""Remy-CC eval — agent A/B and deterministic candidate retrieval benchmarks.

The agent benchmark measures whether attaching Remy-CC's MCP code-intelligence
tools makes a tool-using agent more accurate (F1) and cheaper (tokens /
tool-calls) than a grep/glob/read baseline. The deterministic benchmark records
the current FTS, LIKE, fuzzy, and public fallback candidates against declarative
non-circular ground truth.

The agent methodology follows KBench (github.com/ajksunkang-aios/KBench): same
agent, same task set, the only variable is whether the system-under-test is
attached. Arms: A-baseline (grep/glob/read) vs B-remy (baseline + Remy-CC MCP
tools).
"""
