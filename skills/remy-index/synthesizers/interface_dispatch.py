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


def synthesize_interface_override_edges(db):
    fanout_cap = 10
    try:
        fanout_cap = int(os.environ.get("SYNTH_INTERFACE_FANOUT_CAP", 10))
    except (ValueError, TypeError):
        pass

    classes_with_bases = db.execute(
        "SELECT file_path, name, bases FROM symbols WHERE type = 'class' AND bases IS NOT NULL"
    ).fetchall()

    seen = set()
    for impl_path, impl_class, bases_json in classes_with_bases:
        try:
            bases = json.loads(bases_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not bases:
            continue

        impl_methods = db.execute(
            "SELECT name FROM symbols WHERE file_path = ? AND type = 'function' AND name LIKE ?",
            (impl_path, impl_class + ".%")
        ).fetchall()
        if not impl_methods:
            continue
        impl_method_set = {row[0].split(".")[-1] for row in impl_methods}

        for base_name in bases:
            base_classes = db.execute(
                "SELECT file_path, name FROM symbols WHERE type = 'class' AND short_name = ?",
                (base_name,)
            ).fetchall()

            for base_path, base_full_name in base_classes:
                base_methods = db.execute(
                    "SELECT name, lineno FROM symbols WHERE file_path = ? AND type = 'function' AND name LIKE ?",
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
                    base_qualified = f"{base_path}::{base_method_name}"
                    key = f"{base_qualified}>{impl_qualified}"
                    if key in seen:
                        continue
                    seen.add(key)

                    db.execute(
                        "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified, line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                        (base_path, base_method_name, f"{impl_class}.{method_short}",
                         impl_path, impl_qualified, base_lineno or 0,
                         "heuristic", base_path, "interface-impl")
                    )
                    added += 1

    db.commit()
