"""Current SQLite schema and summary status contract.

The full DDL lives in the sibling schema.sql file — the single source
shared with the Rust scanner (scanner-core embeds it via include_str!).
SCHEMA_SQL keeps its historical value: the byte content of that file.
"""

from pathlib import Path


VERSION = "12.0.0"

# Static-only provenance filter fragment: edge provenance values treated as
# statically resolved. Column name is supplied at each call site.
STATIC_PROVENANCE_SQL = "IN ('definite','probable')"

SCHEMA_SQL = (Path(__file__).resolve().parent / "schema.sql").read_text(
    encoding="utf-8"
)

SUMMARY_STATUS_ENUM = frozenset({'ok', 'pending', 'stale', 'oversized_warn', 'oversized_hard', 'corrupt'})

_STATUS_TRANSITIONS = {
    ('pending', 'llm_success'): 'ok',
    ('pending', 'llm_failure'): 'pending',
    ('pending', 'parse_failure'): 'corrupt',
    ('ok', 'mark_stale'): 'stale',
    ('ok', 'parse_failure'): 'corrupt',
    ('stale', 'rewrite_success'): 'ok',
    ('stale', 'llm_failure'): 'pending',
    ('oversized_warn', 'mark_stale'): 'stale',
    ('oversized_hard', 'mark_stale'): 'stale',
}


def _transition_status(old_status, event):
    if old_status not in SUMMARY_STATUS_ENUM:
        return 'corrupt'
    key = (old_status, event)
    return _STATUS_TRANSITIONS.get(key, old_status)
