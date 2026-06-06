"""
Interface/abstract-method override synthesis.

Bridges the gap where a call to BaseClass.method() dispatches at runtime
to SubClass.method() but no static call edge exists between them.

Synthesizes: base_method -> impl_method for each (base, impl) pair where
the impl class declares the base in its 'bases' field and both have a
method with the same name.
"""

INTERFACE_FANOUT_CAP = 10


def synthesize_interface_override_edges(cache):
    base_index = {}
    for path, file_data in cache.items():
        if path == "_meta":
            continue
        for sym in file_data.get("symbols", []):
            if sym.get("type") == "class":
                name = sym["name"]
                short = name.split(".")[-1] if "." in name else name
                base_index.setdefault(short, []).append((path, sym))

    seen = set()
    for path, file_data in cache.items():
        if path == "_meta":
            continue
        for sym in file_data.get("symbols", []):
            if sym.get("type") != "class":
                continue
            bases = sym.get("bases")
            if not bases:
                continue

            impl_methods = _get_class_methods(file_data, sym["name"])
            if not impl_methods:
                continue

            for base_name in bases:
                base_candidates = base_index.get(base_name, [])
                for base_path, base_sym in base_candidates:
                    base_methods = _get_class_methods(
                        cache.get(base_path, {}), base_sym["name"]
                    )
                    if not base_methods:
                        continue

                    added = 0
                    for method_name, base_method in base_methods.items():
                        if added >= INTERFACE_FANOUT_CAP:
                            break
                        impl_method = impl_methods.get(method_name)
                        if not impl_method:
                            continue

                        base_qualified = f"{base_path}::{base_sym['name']}.{method_name}"
                        impl_qualified = f"{path}::{sym['name']}.{method_name}"
                        key = f"{base_qualified}>{impl_qualified}"
                        if key in seen:
                            continue
                        seen.add(key)

                        edge = {
                            "caller": f"{base_sym['name']}.{method_name}",
                            "callee": f"{sym['name']}.{method_name}",
                            "line": base_method.get("lineno", 0),
                            "provenance": "heuristic",
                            "synthesized_from": base_path,
                            "via": "interface-impl",
                            "callee_qualified": impl_qualified,
                        }
                        cache.setdefault(base_path, {}).setdefault("calls", []).append(edge)
                        added += 1


def _get_class_methods(file_data, class_name):
    methods = {}
    prefix = class_name + "."
    for sym in file_data.get("symbols", []):
        if sym.get("type") != "function":
            continue
        name = sym.get("name", "")
        if name.startswith(prefix):
            method_name = name[len(prefix):]
            if "." not in method_name:
                methods[method_name] = sym
    return methods
