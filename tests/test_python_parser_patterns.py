"""Tests for PythonParser.extract_patterns (Django/PyQt/observer pattern extraction)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from parsers.python_parser import PythonParser


@pytest.fixture
def parser():
    return PythonParser()


class TestDjangoSignals:
    def test_connect_detected(self, parser):
        source = '''
from django.db.models.signals import post_save

def setup():
    post_save.connect(handle_save)

def handle_save(sender, **kwargs):
    pass
'''
        patterns = parser.extract_patterns(source, "signals.py")
        connects = [p for p in patterns if p["pattern_type"] == "django_signal_connect"]
        assert len(connects) == 1
        assert connects[0]["signal_name"] == "post_save"
        assert connects[0]["handler"] == "handle_save"

    def test_send_detected(self, parser):
        source = '''
def notify():
    my_signal.send(sender=self.__class__)
'''
        patterns = parser.extract_patterns(source, "notify.py")
        sends = [p for p in patterns if p["pattern_type"] == "django_signal_send"]
        assert len(sends) == 1
        assert sends[0]["signal_name"] == "my_signal"
        assert sends[0]["handler"] == "notify"

    def test_no_signals_empty_result(self, parser):
        source = 'def plain():\n    return 1\n'
        patterns = parser.extract_patterns(source, "plain.py")
        assert patterns == []


class TestPyqtSignals:
    def test_connect_detected(self, parser):
        source = '''
from PyQt5.QtCore import pyqtSignal

class MyWidget:
    def setup(self):
        self.clicked.connect(self.on_click)

    def on_click(self):
        pass
'''
        patterns = parser.extract_patterns(source, "widget.py")
        connects = [p for p in patterns if p["pattern_type"] == "pyqt_signal_connect"]
        assert len(connects) == 1
        assert connects[0]["signal_name"] == "clicked"
        assert connects[0]["handler"] == "on_click"

    def test_emit_detected(self, parser):
        source = '''
from PySide2.QtCore import Signal

class Emitter:
    def fire(self):
        self.data_ready.emit(42)
'''
        patterns = parser.extract_patterns(source, "emitter.py")
        emits = [p for p in patterns if p["pattern_type"] == "pyqt_signal_emit"]
        assert len(emits) == 1
        assert emits[0]["signal_name"] == "data_ready"

    def test_no_pyqt_import_no_detection(self, parser):
        source = '''
class Fake:
    def setup(self):
        self.signal.connect(self.handler)
'''
        patterns = parser.extract_patterns(source, "fake.py")
        pyqt = [p for p in patterns if "pyqt" in p["pattern_type"]]
        assert len(pyqt) == 0


class TestObserverPattern:
    def test_register_detected(self, parser):
        source = '''
class EventBus:
    def subscribe(self, callback):
        self.listeners.append(callback)
'''
        patterns = parser.extract_patterns(source, "bus.py")
        registers = [p for p in patterns if p["pattern_type"] == "observer_register"]
        assert len(registers) == 1
        assert registers[0]["signal_name"] == "listeners"

    def test_emit_detected(self, parser):
        source = '''
class EventBus:
    def dispatch(self):
        for cb in self.handlers:
            cb()
'''
        patterns = parser.extract_patterns(source, "bus.py")
        emits = [p for p in patterns if p["pattern_type"] == "observer_emit"]
        assert len(emits) == 1
        assert emits[0]["signal_name"] == "handlers"

    def test_non_invoke_loop_ignored(self, parser):
        source = '''
class Printer:
    def show(self):
        for item in self.items:
            print(item)
'''
        patterns = parser.extract_patterns(source, "printer.py")
        emits = [p for p in patterns if p["pattern_type"] == "observer_emit"]
        assert len(emits) == 0


class TestTreeCacheOnSyntaxError:
    """Regression: the tree cache must never serve a stale AST.

    Before the R3.3 fix, _get_tree stored the source hash before ast.parse
    could raise, so every later channel call for the same broken source hit
    the cache and received the previously parsed file's tree (scan order
    dependent facts) or None (AttributeError on the first file).
    """

    BAD = "def broken(:\n"

    def test_broken_source_yields_empty_facts_after_a_good_file(self, parser):
        good = "def good_fn():\n    good_call()\n"
        assert [s.name for s in parser.parse_symbols(good, "good.py")] == ["good_fn"]
        assert parser.resolve_imports(self.BAD, "bad.py", ".") == {}
        assert parser.collect_import_bindings(self.BAD, "bad.py", ".") == []
        assert parser.parse_symbols(self.BAD, "bad.py") == []
        assert parser.extract_call_graph(self.BAD, "bad.py") == []

    def test_broken_source_on_fresh_parser_does_not_raise(self, parser):
        assert parser.resolve_imports(self.BAD, "bad.py", ".") == {}
        assert parser.collect_import_bindings(self.BAD, "bad.py", ".") == []

    def test_good_source_still_parses_after_a_broken_file(self, parser):
        assert parser.parse_symbols(self.BAD, "bad.py") == []
        good = "def after_fn():\n    pass\n"
        assert [s.name for s in parser.parse_symbols(good, "good.py")] == ["after_fn"]

