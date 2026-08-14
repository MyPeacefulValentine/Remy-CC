"""
Rust trait-impl override synthesis (SQL implementation).

Bridges the gap where a call to Trait::method() dispatches at runtime to
Type::method() but no static call edge exists between them.

Synthesizes: trait_method -> impl_method for each (trait, type) pair where
the type symbol carries the trait in its 'bases' field (attached by
RustParser from same-file `impl Trait for Type` blocks) and both sides have
a method with the same name. Scoped to .rs files on both sides so existing
language baselines are unaffected.
"""

import json
import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config


def synthesize_rust_trait_impl_edges(db):
    fanout_cap = remy_config.load_config(strict=True).get_int("REMY_SYNTH_INTERFACE_FANOUT_CAP")

    impl_types = db.execute(
        "SELECT file_path, name, bases FROM symbols "
        "WHERE type IN ('struct', 'enum') AND bases IS NOT NULL "
        "AND file_path LIKE '%.rs' ORDER BY file_path, name"
    ).fetchall()

    seen = set()
    inserted = 0
    for impl_path, impl_type, bases_json in impl_types:
        try:
            bases = json.loads(bases_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not bases:
            continue

        impl_methods = db.execute(
            "SELECT name FROM symbols WHERE file_path = ? AND type = 'function' "
            "AND name LIKE ? ORDER BY name",
            (impl_path, impl_type + ".%")
        ).fetchall()
        if not impl_methods:
            continue
        impl_method_set = {row[0].split(".")[-1] for row in impl_methods}

        for trait_name in sorted(set(bases)):
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

                    impl_qualified = f"{impl_path}::{impl_type}.{method_short}"
                    key = (trait_path, trait_method_name, impl_qualified, "trait-impl")
                    if key in seen:
                        continue
                    seen.add(key)

                    cursor = db.execute(
                        "INSERT OR IGNORE INTO edges "
                        "(source_file, caller, callee, callee_file, callee_qualified, "
                        "line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                        (trait_path, trait_method_name, f"{impl_type}.{method_short}",
                         impl_path, impl_qualified, trait_lineno or 0,
                         "inferred", trait_path, "trait-impl")
                    )
                    inserted += max(cursor.rowcount, 0)
                    added += 1
    return inserted
