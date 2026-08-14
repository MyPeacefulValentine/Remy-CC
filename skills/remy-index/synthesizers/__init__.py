"""
Callback/observer edge synthesis — post-extraction pass.

Closes dynamic-dispatch holes where a dispatcher invokes callbacks registered
elsewhere. All synthesized edges carry provenance='inferred'.
"""

from .event_emitter import synthesize_event_emitter_edges
from .interface_dispatch import synthesize_interface_override_edges
from .c_fnptr_dispatch import synthesize_c_fnptr_dispatch_edges
from .rust_trait_dispatch import synthesize_rust_trait_impl_edges


def run_all_synthesizers(db):
    return {
        "event_emitter": synthesize_event_emitter_edges(db),
        "interface_dispatch": synthesize_interface_override_edges(db),
        "c_fnptr_dispatch": synthesize_c_fnptr_dispatch_edges(db),
        "rust_trait_dispatch": synthesize_rust_trait_impl_edges(db),
    }
