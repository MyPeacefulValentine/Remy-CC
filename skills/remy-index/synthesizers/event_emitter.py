"""
Event/observer edge synthesis for Python projects.

Covers three patterns:
  - Django/blinker signals (signal.connect / signal.send)
  - PyQt/PySide signals (signal.connect / signal.emit)
  - Custom observer (self.FIELD.append(cb) / for cb in self.FIELD: cb())
"""

import re
import os

SIGNAL_FANOUT_CAP = 8
OBSERVER_FANOUT_CAP = 6

DJANGO_CONNECT_RE = re.compile(r'(\w+)\.connect\(\s*(\w+)')
DJANGO_SEND_RE = re.compile(r'(\w+)\.send\(')

PYQT_CONNECT_RE = re.compile(r'(\w+)\.connect\(\s*(?:self\.)?(\w+)')
PYQT_EMIT_RE = re.compile(r'(\w+)\.emit\(')

OBSERVER_ITER_DISPATCH_RE = re.compile(
    r'for\s+(\w+)\s+in\s+self\.(\w+)\s*:',
)
OBSERVER_INVOKE_RE = re.compile(r'\b(\w+)\s*\(')
OBSERVER_APPEND_RE = re.compile(r'self\.(\w+)\.(?:append|add|insert)\(')


def _has_django_gate(source):
    return '.connect(' in source or '.send(' in source


def _has_pyqt_gate(source):
    return ('from PyQt' in source or 'from PySide' in source) and (
        '.connect(' in source or '.emit(' in source
    )


def _find_enclosing_function(symbols, line):
    best = None
    for sym in symbols:
        if sym.get("type") != "function":
            continue
        start = sym.get("lineno", 0)
        end = sym.get("end_lineno") or start
        if start <= line <= end:
            if best is None or start >= best.get("lineno", 0):
                best = sym
    return best


def _line_number_at(source, pos):
    return source[:pos].count('\n') + 1


def synthesize_event_emitter_edges(cache, root_dir):
    _synthesize_django_signals(cache, root_dir)
    _synthesize_pyqt_signals(cache, root_dir)
    _synthesize_observer_pattern(cache, root_dir)


def _read_source(root_dir, rel_path):
    try:
        with open(os.path.join(root_dir, rel_path), 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return None


def _synthesize_django_signals(cache, root_dir):
    emit_map = {}
    handler_map = {}

    for path, file_data in cache.items():
        if path == "_meta":
            continue
        symbols = file_data.get("symbols", [])
        source = _read_source(root_dir, path)
        if not source:
            continue
        if not _has_django_gate(source):
            continue

        for m in DJANGO_SEND_RE.finditer(source):
            signal_name = m.group(1)
            line = _line_number_at(source, m.start())
            fn = _find_enclosing_function(symbols, line)
            if fn:
                emit_map.setdefault(signal_name, []).append(
                    (path, fn["name"], line)
                )

        for m in DJANGO_CONNECT_RE.finditer(source):
            signal_name = m.group(1)
            handler_name = m.group(2)
            handler_map.setdefault(signal_name, []).append(
                (path, handler_name, _line_number_at(source, m.start()))
            )

    _pair_and_inject(cache, emit_map, handler_map, SIGNAL_FANOUT_CAP, "django-signal")


def _synthesize_pyqt_signals(cache, root_dir):
    emit_map = {}
    handler_map = {}

    for path, file_data in cache.items():
        if path == "_meta":
            continue
        symbols = file_data.get("symbols", [])
        source = _read_source(root_dir, path)
        if not source:
            continue
        if not _has_pyqt_gate(source):
            continue

        for m in PYQT_EMIT_RE.finditer(source):
            signal_name = m.group(1)
            line = _line_number_at(source, m.start())
            fn = _find_enclosing_function(symbols, line)
            if fn:
                emit_map.setdefault(signal_name, []).append(
                    (path, fn["name"], line)
                )

        for m in PYQT_CONNECT_RE.finditer(source):
            signal_name = m.group(1)
            handler_name = m.group(2)
            handler_map.setdefault(signal_name, []).append(
                (path, handler_name, _line_number_at(source, m.start()))
            )

    _pair_and_inject(cache, emit_map, handler_map, SIGNAL_FANOUT_CAP, "pyqt-signal")


def _synthesize_observer_pattern(cache, root_dir):
    dispatch_map = {}
    register_map = {}

    for path, file_data in cache.items():
        if path == "_meta":
            continue
        symbols = file_data.get("symbols", [])
        source = _read_source(root_dir, path)
        if not source:
            continue

        for m in OBSERVER_ITER_DISPATCH_RE.finditer(source):
            loop_var = m.group(1)
            field_name = m.group(2)
            after_colon = source[m.end():]
            if OBSERVER_INVOKE_RE.match(after_colon.lstrip()):
                invoke_match = OBSERVER_INVOKE_RE.search(after_colon)
                if invoke_match and invoke_match.group(1) == loop_var:
                    line = _line_number_at(source, m.start())
                    fn = _find_enclosing_function(symbols, line)
                    if fn:
                        dispatch_map.setdefault(field_name, []).append(
                            (path, fn["name"], line)
                        )

        for m in OBSERVER_APPEND_RE.finditer(source):
            field_name = m.group(1)
            line = _line_number_at(source, m.start())
            fn = _find_enclosing_function(symbols, line)
            if fn:
                register_map.setdefault(field_name, []).append(
                    (path, fn["name"], line)
                )

    _pair_and_inject(cache, dispatch_map, register_map, OBSERVER_FANOUT_CAP, "observer")


def _pair_and_inject(cache, emit_map, handler_map, fanout_cap, via_label):
    seen = set()
    for channel, dispatchers in emit_map.items():
        handlers = handler_map.get(channel)
        if not handlers:
            continue
        if len(dispatchers) > fanout_cap or len(handlers) > fanout_cap:
            continue
        for d_path, d_func, d_line in dispatchers:
            for h_path, h_func, _h_line in handlers:
                if d_path == h_path and d_func == h_func:
                    continue
                key = f"{d_path}::{d_func}>{h_path}::{h_func}"
                if key in seen:
                    continue
                seen.add(key)
                edge = {
                    "caller": d_func,
                    "callee": h_func,
                    "line": d_line,
                    "provenance": "heuristic",
                    "synthesized_from": d_path,
                    "via": via_label,
                    "callee_qualified": f"{h_path}::{h_func}",
                }
                cache.setdefault(d_path, {}).setdefault("calls", []).append(edge)
