"""C/C++ function-pointer dispatch synthesis (SQL post-pass).

Reconstructs dispatcher->handler `calls` edges that static extraction misses:
functions registered into a struct's fn-pointer field via a table, then called
indirectly (`table[i].field(arg)`). Consumes the c_fnptr_* facts from
CCppParser.extract_patterns, resolves them cross-file, and writes edges with
provenance='inferred', via='c-fnptr-dispatch'.

Design, coverage boundary, and roadmap: plans/remy-index-evolution-plan.md.
"""

import json

from .c_fnptr_profiles import get_profile


def synthesize_c_fnptr_dispatch_edges(db, profile_name="tee"):
    profile = get_profile(profile_name)
    fanout_cap = profile.get("fanout_cap", 300)

    fnptr_typedefs = set()
    for (sig,) in db.execute(
        "SELECT signal_name FROM patterns WHERE pattern_type = 'c_fnptr_typedef'"
    ).fetchall():
        if sig:
            fnptr_typedefs.add(sig)

    # struct name -> list of distinct field layouts (a name may recur across files)
    layouts = {}
    for sig, meta in db.execute(
        "SELECT signal_name, metadata FROM patterns WHERE pattern_type = 'c_struct_layout'"
    ).fetchall():
        if not sig or not meta:
            continue
        try:
            fields = json.loads(meta).get("fields", [])
        except (json.JSONDecodeError, TypeError):
            continue
        layouts.setdefault(sig, [])
        if fields and fields not in layouts[sig]:
            layouts[sig].append(fields)

    def _field_fnptr(field):
        return bool(field.get("is_fnptr") or field.get("type") in fnptr_typedefs)

    def resolve_reg_field(struct, field_name=None, slot=None):
        """
        Return the fn-pointer field name a registration targets, or None if the
        slot/field is not a fn-pointer field of `struct` in any known layout.
        """
        for fields in layouts.get(struct, []):
            for f in fields:
                if field_name is not None and f.get("name") == field_name and _field_fnptr(f):
                    return f.get("name")
                if slot is not None and f.get("index") == slot and _field_fnptr(f):
                    return f.get("name")
        return None

    # fn-pointer field name -> set of structs that declare it
    field_to_structs = {}
    for struct, layout_list in layouts.items():
        for fields in layout_list:
            for f in fields:
                if f.get("name") and _field_fnptr(f):
                    field_to_structs.setdefault(f["name"], set()).add(struct)

    # (struct, field) -> set of registered handler names
    reg = {}
    for sig, handler, meta in db.execute(
        "SELECT signal_name, handler, metadata FROM patterns WHERE pattern_type = 'c_fnptr_register'"
    ).fetchall():
        if not sig or not handler or not meta:
            continue
        try:
            md = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            continue
        field = resolve_reg_field(sig, field_name=md.get("field"), slot=md.get("slot"))
        if field:
            reg.setdefault((sig, field), set()).add(handler)

    if not reg:
        return

    _symbol_cache = {}

    def resolve_symbol(name, prefer_file=None):
        rows = _symbol_cache.get(name)
        if rows is None:
            rows = db.execute(
                "SELECT file_path, name FROM symbols WHERE (short_name = ? OR name = ?) "
                "AND type IN ('function', 'macro')",
                (name, name)
            ).fetchall()
            _symbol_cache[name] = rows
        if not rows:
            return None
        if prefer_file:
            for fp, nm in rows:
                if fp == prefer_file:
                    return fp, nm
        return rows[0]

    seen = set()
    inserted = 0
    for field, enclosing, meta, disp_file, line in db.execute(
        "SELECT signal_name, handler, metadata, file_path, line "
        "FROM patterns WHERE pattern_type = 'c_fnptr_dispatch'"
    ).fetchall():
        if not field or not enclosing:
            continue
        owners = field_to_structs.get(field)
        if not owners:
            continue
        try:
            struct_hint = (json.loads(meta) or {}).get("struct_hint") if meta else None
        except (json.JSONDecodeError, TypeError):
            struct_hint = None
        if struct_hint and struct_hint in owners:
            struct = struct_hint
        elif len(owners) == 1:
            struct = next(iter(owners))
        else:
            continue
        targets = reg.get((struct, field))
        if not targets:
            continue
        caller = resolve_symbol(enclosing, prefer_file=disp_file)
        if not caller:
            continue
        caller_file, caller_name = caller
        added = 0
        for handler in sorted(targets):
            if added >= fanout_cap:
                break
            callee = resolve_symbol(handler)
            if not callee:
                continue
            callee_file, callee_name = callee
            if caller_file == callee_file and caller_name == callee_name:
                continue
            key = f"{caller_file}::{caller_name}>{callee_file}::{callee_name}"
            if key in seen:
                continue
            seen.add(key)
            db.execute(
                "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified, "
                "line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                (caller_file, caller_name, callee_name.split(".")[-1], callee_file,
                 f"{callee_file}::{callee_name}", line or 0,
                 "inferred", disp_file, "c-fnptr-dispatch")
            )
            added += 1
            inserted += 1

    if inserted:
        db.commit()
