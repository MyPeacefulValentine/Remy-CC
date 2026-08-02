"""
Event/observer edge synthesis via patterns table (SQL implementation).

Covers three pattern families stored in the patterns table:
  - Django/blinker signals (django_signal_connect / django_signal_send)
  - PyQt/PySide signals (pyqt_signal_connect / pyqt_signal_emit)
  - Custom observer (observer_register / observer_emit)
"""

import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config


def synthesize_event_emitter_edges(db):
    fanout_cap = remy_config.load_config(strict=True).get_int("REMY_SYNTH_EVENT_FANOUT_CAP")

    inserted = 0
    inserted += _synthesize_signal_pattern(
        db, "django_signal_send", "django_signal_connect", "django-signal", fanout_cap
    )
    inserted += _synthesize_signal_pattern(
        db, "pyqt_signal_emit", "pyqt_signal_connect", "pyqt-signal", fanout_cap
    )
    inserted += _synthesize_signal_pattern(
        db, "observer_emit", "observer_register", "observer", fanout_cap
    )
    return inserted


def _synthesize_signal_pattern(db, emit_type, connect_type, via_label, fanout_cap):
    emitters = db.execute(
        "SELECT file_path, signal_name, handler, line FROM patterns "
        "WHERE pattern_type = ? "
        "ORDER BY COALESCE(line, 0), file_path, signal_name, handler",
        (emit_type,),
    ).fetchall()

    if not emitters:
        return 0

    signal_names = sorted({row[1] for row in emitters if row[1]})
    if not signal_names:
        return 0

    placeholders = ','.join(['?'] * len(signal_names))
    handlers = db.execute(
        f"SELECT file_path, signal_name, handler, line FROM patterns "
        f"WHERE pattern_type = ? AND signal_name IN ({placeholders}) "
        "ORDER BY signal_name, COALESCE(line, 0), file_path, handler",
        [connect_type] + signal_names,
    ).fetchall()

    handler_map = {}
    for h_path, h_signal, h_func, h_line in handlers:
        handler_map.setdefault(h_signal, []).append((h_path, h_func, h_line))

    seen = set()
    inserted = 0
    for e_path, e_signal, e_func, e_line in emitters:
        if not e_signal or not e_func:
            continue
        targets = handler_map.get(e_signal)
        if not targets:
            continue
        if len(targets) > fanout_cap:
            continue

        for h_path, h_func, _ in targets:
            if not h_func:
                continue
            if e_path == h_path and e_func == h_func:
                continue
            key = (e_path, e_func, f"{h_path}::{h_func}", via_label)
            if key in seen:
                continue
            seen.add(key)

            cursor = db.execute(
                "INSERT OR IGNORE INTO edges "
                "(source_file, caller, callee, callee_file, callee_qualified, "
                "line, provenance, synthesized_from, via) VALUES (?,?,?,?,?,?,?,?,?)",
                (e_path, e_func, h_func, h_path, f"{h_path}::{h_func}",
                 e_line or 0, "inferred", e_path, via_label)
            )
            inserted += max(cursor.rowcount, 0)
    return inserted
