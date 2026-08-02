"""Shared constants for the remy-index module.

Centralizes default values consumed by both run.py and bootstrap.py so
that callers in either entry-path see the same defaults without
introducing a reverse import between the main pipeline and the
hierarchical-summary bootstrap.
"""

DB_BUSY_TIMEOUT_MS = 5000
DB_CONNECT_TIMEOUT_S = 10
