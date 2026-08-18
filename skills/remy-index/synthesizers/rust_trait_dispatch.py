"""
Rust trait-impl override synthesis (SQL implementation).

Bridges the gap where a call to Trait::method() dispatches at runtime to
Type::method() but no static call edge exists between them.

Consumes the per-file ``rust_trait_impl`` facts emitted by RustParser (one
row per ``impl Trait for Type`` block, carrying the impl site), resolves the
trait globally by short name among .rs interface symbols, and synthesizes
trait_method -> impl_method edges for each method the impl file defines
under the impl-site type prefix. The impl site is authoritative for method
lookup, so cross-file impl blocks resolve without a same-file type symbol.
"""

import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config


def synthesize_rust_trait_impl_edges(db):
    fanout_cap = remy_config.load_config(strict=True).get_int("REMY_SYNTH_INTERFACE_FANOUT_CAP")

    impls = db.execute(
        "SELECT file_path, signal_name, handler FROM patterns "
        "WHERE pattern_type = 'rust_trait_impl' "
        "AND signal_name IS NOT NULL AND handler IS NOT NULL "
        "ORDER BY file_path, COALESCE(line, 0), signal_name, handler"
    ).fetchall()

    seen = set()
    inserted = 0
    for impl_file, trait_name, full_type in impls:
        impl_methods = db.execute(
            "SELECT name FROM symbols WHERE file_path = ? AND type = 'function' "
            "AND name LIKE ? ORDER BY name",
            (impl_file, full_type + ".%")
        ).fetchall()
        if not impl_methods:
            continue
        impl_method_set = {row[0].split(".")[-1] for row in impl_methods}

        traits = db.execute(
            "SELECT file_path, name FROM symbols WHERE type = 'interface' "
            "AND short_name = ? AND file_path LIKE '%.rs' "
            "ORDER BY file_path, name",
            (trait_name,)
        ).fetchall()

        for trait_path, trait_full_name in traits:
            trait_methods = db.execute(
                "SELECT name, lineno FROM symbols WHERE file_path = ? "
                "AND type = 'function' AND name LIKE ? "
                "ORDER BY COALESCE(lineno, 0), name",
                (trait_path, trait_full_name + ".%")
            ).fetchall()
            if not trait_methods:
                continue

            added = 0
            for trait_method_name, trait_lineno in trait_methods:
                if added >= fanout_cap:
                    break
                method_short = trait_method_name.split(".")[-1]
                if method_short not in impl_method_set:
                    continue

                impl_qualified = f"{impl_file}::{full_type}.{method_short}"
                key = (trait_path, trait_method_name, impl_qualified, "trait-impl")
                if key in seen:
                    continue
                seen.add(key)

                cursor = db.execute(
                    "INSERT OR IGNORE INTO edges "
                    "(source_file, caller, callee, callee_file, callee_qualified, "
                    "line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                    (trait_path, trait_method_name, f"{full_type}.{method_short}",
                     impl_file, impl_qualified, trait_lineno or 0,
                     "inferred", trait_path, "trait-impl")
                )
                inserted += max(cursor.rowcount, 0)
                added += 1
    return inserted
