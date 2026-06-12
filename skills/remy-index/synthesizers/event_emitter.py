"""
Event/observer edge synthesis via patterns table (SQL implementation).

Covers three pattern families stored in the patterns table:
  - Django/blinker signals (django_signal_connect / django_signal_send)
  - PyQt/PySide signals (pyqt_signal_connect / pyqt_signal_emit)
  - Custom observer (observer_register / observer_emit)
"""

import os


def synthesize_event_emitter_edges(db):
    fanout_cap = 8
    try:
        fanout_cap = int(os.environ.get("SYNTH_EVENT_FANOUT_CAP", 20))
    except (ValueError, TypeError):
        pass

    _synthesize_signal_pattern(db, "django_signal_send", "django_signal_connect", "django-signal", fanout_cap)
    _synthesize_signal_pattern(db, "pyqt_signal_emit", "pyqt_signal_connect", "pyqt-signal", fanout_cap)
    _synthesize_signal_pattern(db, "observer_emit", "observer_register", "observer", fanout_cap)


def _synthesize_signal_pattern(db, emit_type, connect_type, via_label, fanout_cap):
    emitters = db.execute(
        "SELECT file_path, signal_name, handler, line FROM patterns WHERE pattern_type = ?",
        (emit_type,)
    ).fetchall()

    if not emitters:
        return

    signal_names = list({row[1] for row in emitters if row[1]})
    if not signal_names:
        return

    placeholders = ','.join(['?'] * len(signal_names))
    handlers = db.execute(
        f"SELECT file_path, signal_name, handler, line FROM patterns WHERE pattern_type = ? AND signal_name IN ({placeholders})",
        [connect_type] + signal_names
    ).fetchall()

    handler_map = {}
    for h_path, h_signal, h_func, h_line in handlers:
        handler_map.setdefault(h_signal, []).append((h_path, h_func, h_line))

    seen = set()
    for e_path, e_signal, e_func, e_line in emitters:
        if not e_signal or not e_func:
            continue
        targets = handler_map.get(e_signal)
        if not targets:
            continue
        if len(targets) > fanout_cap:
            continue

        for h_path, h_func, _h_line in targets:
            if not h_func:
                continue
            if e_path == h_path and e_func == h_func:
                continue
            key = f"{e_path}::{e_func}>{h_path}::{h_func}"
            if key in seen:
                continue
            seen.add(key)

            db.execute(
                "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified, line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                (e_path, e_func, h_func, h_path, f"{h_path}::{h_func}",
                 e_line or 0, "heuristic", e_path, via_label)
            )

    db.commit()
