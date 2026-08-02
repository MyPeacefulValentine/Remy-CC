"""
Interface/abstract-method override synthesis (SQL implementation).

Bridges the gap where a call to BaseClass.method() dispatches at runtime
to SubClass.method() but no static call edge exists between them.

Synthesizes: base_method -> impl_method for each (base, impl) pair where
the impl class declares the base in its 'bases' field and both have a
method with the same name.
"""

import json
import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config


def synthesize_interface_override_edges(db):
    fanout_cap = remy_config.load_config(strict=True).get_int("REMY_SYNTH_INTERFACE_FANOUT_CAP")

    classes_with_bases = db.execute(
        "SELECT file_path, name, bases FROM symbols "
        "WHERE type = 'class' AND bases IS NOT NULL ORDER BY file_path, name"
    ).fetchall()

    seen = set()
    inserted = 0
    for impl_path, impl_class, bases_json in classes_with_bases:
        try:
            bases = json.loads(bases_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not bases:
            continue

        impl_methods = db.execute(
            "SELECT name FROM symbols WHERE file_path = ? AND type = 'function' "
            "AND name LIKE ? ORDER BY name",
            (impl_path, impl_class + ".%")
        ).fetchall()
        if not impl_methods:
            continue
        impl_method_set = {row[0].split(".")[-1] for row in impl_methods}

        for base_name in sorted(set(bases)):
            base_classes = db.execute(
                "SELECT file_path, name FROM symbols WHERE type = 'class' "
                "AND short_name = ? ORDER BY file_path, name",
                (base_name,)
            ).fetchall()

            for base_path, base_full_name in base_classes:
                base_methods = db.execute(
                    "SELECT name, lineno FROM symbols WHERE file_path = ? "
                    "AND type = 'function' AND name LIKE ? "
                    "ORDER BY COALESCE(lineno, 0), name",
                    (base_path, base_full_name + ".%")
                ).fetchall()
                if not base_methods:
                    continue

                added = 0
                for base_method_name, base_lineno in base_methods:
                    if added >= fanout_cap:
                        break
                    method_short = base_method_name.split(".")[-1]
                    if method_short not in impl_method_set:
                        continue

                    impl_qualified = f"{impl_path}::{impl_class}.{method_short}"
                    key = (base_path, base_method_name, impl_qualified, "interface-impl")
                    if key in seen:
                        continue
                    seen.add(key)

                    cursor = db.execute(
                        "INSERT OR IGNORE INTO edges "
                        "(source_file, caller, callee, callee_file, callee_qualified, "
                        "line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                        (base_path, base_method_name, f"{impl_class}.{method_short}",
                         impl_path, impl_qualified, base_lineno or 0,
                         "inferred", base_path, "interface-impl")
                    )
                    inserted += max(cursor.rowcount, 0)
                    added += 1
    return inserted
