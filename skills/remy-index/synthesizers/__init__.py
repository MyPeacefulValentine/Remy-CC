"""
Callback/observer edge synthesis — post-extraction pass.

Closes dynamic-dispatch holes where a dispatcher invokes callbacks registered
elsewhere. All synthesized edges carry provenance='heuristic'.
"""

from .event_emitter import synthesize_event_emitter_edges
from .interface_dispatch import synthesize_interface_override_edges


def run_all_synthesizers(db):
    synthesize_event_emitter_edges(db)
    synthesize_interface_override_edges(db)
